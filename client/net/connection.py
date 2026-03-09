# net/connection.py
"""NetConnection handler."""
from __future__ import annotations

from typing import Any, Callable, Optional

from serialization.bit_reader import FBitReader
from serialization.bit_writer import FBitWriter
from serialization.bit_util import FBitUtil
from net.packets.out_bunch import FOutBunch
from net.packets.in_bunch import FInBunch
from net.reliability.packet_notify import FNetPacketNotify, FNotificationHeader
from net.reliability.sequence_number import SequenceNumber
from core.names.ename import EName
from core.names.fname import FName
from net.channels.channel_types import EChannelCloseReason
from net.channels.base_channel import Channel
from net.channels.channel_registry import (
    create_channel,
    ensure_default_channels_registered,
    get_channel_type,
)
from net.guid.package_map_client import (
    create_package_map_state,
    use_package_map_state,
)
from net.error_reporter import report_exception, PARSE_EXCEPTIONS
from constants import (
    MAX_CHANNELS, MIN_BUNCH_HEADER_BITS, MAX_BUNCH_HEADER_BITS, MAX_BUNCH_DATA_BITS,
    MAX_JITTER_CLOCK_TIME_VALUE, MAX_CHSEQUENCE, MAX_CHSEQUENCE_HALF, MAX_CHSEQUENCE_MASK,
    CLOSE_REASON_MAX, ENGINE_NET_VER_CURRENT,
    ENGINE_NET_VER_CHANNEL_CLOSE_REASON, ENGINE_NET_VER_JITTER_IN_HEADER,
)


class NetConnection:
    __slots__ = (
        'local_network_version', 'cached_client_id',
        'packet_notify', 'in_packet_id', 'out_packet_id', 'out_ack_packet_id',
        'in_reliable', 'out_reliable',
        'max_bunch_header_bits', 'max_bunch_payload_bits',
        'channels', 'total_bunches_received', 'bunches_with_exports',
        'b_internal_ack', 'handler_components',
        'extensions', 'package_map_state',
        'b_closed', 'close_reason', 'engine_net_ver',
        'channel_record', 'resend_packets',
    )

    def __init__(
        self,
        cached_client_id: int = 0,
        initial_in_seq: int = 0,
        initial_out_seq: int = 0,
        local_network_version: int = 0,
    ):
        ensure_default_channels_registered()

        self.local_network_version = local_network_version
        self.cached_client_id = cached_client_id

        self.packet_notify = FNetPacketNotify()
        self.packet_notify.init(
            SequenceNumber(initial_in_seq),
            SequenceNumber(initial_out_seq)
        )

        self.in_packet_id = initial_in_seq - 1
        self.out_packet_id = initial_out_seq
        self.out_ack_packet_id = initial_out_seq - 1

        self.in_reliable = [initial_in_seq & MAX_CHSEQUENCE_MASK] * MAX_CHANNELS
        self.out_reliable = [initial_out_seq & MAX_CHSEQUENCE_MASK] * MAX_CHANNELS

        self.max_bunch_header_bits = MAX_BUNCH_HEADER_BITS
        self.max_bunch_payload_bits = MAX_BUNCH_DATA_BITS

        self.channels: list[Optional[Channel]] = [None] * MAX_CHANNELS

        self.total_bunches_received = 0
        self.bunches_with_exports = 0

        self.b_internal_ack = False
        self.handler_components = []

        self.extensions: dict[str, Any] = {}
        self.package_map_state = create_package_map_state()

        self.b_closed: bool = False
        self.close_reason: str = ""
        self.engine_net_ver: int = ENGINE_NET_VER_CURRENT

        self.channel_record: dict[int, list[int]] = {}
        self.resend_packets: list[bytes] = []

    def set_handlers(self, handlers: list):
        self.handler_components = handlers

    def set_encryption_key(self, key: bytes) -> None:
        raise NotImplementedError("AESGCM encryption is not enabled for Lyra")

    def enable_encryption(self) -> None:
        raise NotImplementedError("AESGCM encryption is not enabled for Lyra")

    def get_extension(self, key: str, factory: Callable[[], Any]) -> Any:
        value = self.extensions.get(key)
        if value is None:
            value = factory()
            self.extensions[key] = value
        return value

    def is_internal_ack(self) -> bool:
        return self.b_internal_ack

    def _is_valid_channel_index(self, index: int) -> bool:
        return 0 <= index < len(self.channels)

    def create_disconnect_packet(self) -> bytes:
        """Create packet to close Control channel and disconnect."""

        send_buffer = self.init_send_buffer(80)
        bunch = FOutBunch(self.max_bunch_payload_bits)
        bunch.bReliable = True
        bunch.bClose = True
        bunch.CloseReason = EChannelCloseReason.Destroyed
        bunch.ChIndex = 0
        bunch.ChNameIndex = EName.Control
        return self.get_raw_bunch(bunch, send_buffer)

    def get_raw_bunch(self, bunch: FOutBunch, send_buffer_writer: FBitWriter) -> bytes:
        if not self._is_valid_channel_index(bunch.ChIndex):
            raise ValueError(f"Outgoing bunch channel index out of bounds: {bunch.ChIndex}")

        if bunch.bReliable and not self.is_internal_ack():
            self.out_reliable[bunch.ChIndex] = (self.out_reliable[bunch.ChIndex] + 1) & MAX_CHSEQUENCE_MASK
            bunch.ChSequence = self.out_reliable[bunch.ChIndex]

        pkt = self.write_bunch_to_send_buffer(bunch, send_buffer_writer)
        bunch.PacketId = self.out_packet_id

        if bunch.bReliable and not self.is_internal_ack():
            channel = self.channels[bunch.ChIndex]
            if channel is not None:
                channel.add_out_rec(bunch)
            self.channel_record.setdefault(bunch.PacketId, []).append(bunch.ChIndex)

        return pkt

    def write_bunch_to_send_buffer(self, bunch: FOutBunch, send_buffer_writer: FBitWriter) -> bytes:
        header_writer = FBitWriter(self.max_bunch_header_bits)

        header_writer.write_bit(bunch.bOpen or bunch.bClose)

        if bunch.bOpen or bunch.bClose:
            header_writer.write_bit(bunch.bOpen)
            header_writer.write_bit(bunch.bClose)
            if bunch.bClose:
                header_writer.serialize_int(bunch.CloseReason, CLOSE_REASON_MAX)

        header_writer.write_bit(bunch.bIsReplicationPaused)
        header_writer.write_bit(bunch.bReliable)
        header_writer.write_uint32_packed(bunch.ChIndex)
        header_writer.write_bit(bunch.bHasPackageMapExports)
        header_writer.write_bit(bunch.bHasMustBeMappedGUIDs)
        header_writer.write_bit(bunch.bPartial)

        if bunch.bReliable and not self.is_internal_ack():
            header_writer.write_int_wrapped(bunch.ChSequence, MAX_CHSEQUENCE)

        if bunch.bPartial:
            header_writer.write_bit(bunch.bPartialInitial)
            header_writer.write_bit(bunch.bPartialCustomExportsFinal)
            header_writer.write_bit(bunch.bPartialFinal)

        if bunch.bOpen or bunch.bReliable:
            header_writer.serialize_bits(bytes([1]), 1)
            header_writer.write_uint32_packed(bunch.ChNameIndex)

        header_writer.write_int_wrapped(bunch.num_bits, self.max_bunch_payload_bits)

        send_buffer_writer.serialize_bits(header_writer.get_buffer(), header_writer.num_bits)
        send_buffer_writer.serialize_bits(bunch.get_buffer(), bunch.num_bits)

        return self._finalize_send_buffer(send_buffer_writer)

    def init_send_buffer(self, clock_time_ms: int = 0) -> FBitWriter:
        send_buffer_writer = FBitWriter(allow_resize=True)

        self.packet_notify.write_header(send_buffer_writer)

        has_packet_info = clock_time_ms > 0
        send_buffer_writer.write_bit(has_packet_info)
        if has_packet_info:
            send_buffer_writer.serialize_int(clock_time_ms, MAX_JITTER_CLOCK_TIME_VALUE + 1)
            send_buffer_writer.write_bit(False)

        self.packet_notify.commit_and_increment_out_seq()
        self.out_packet_id += 1

        return send_buffer_writer

    def create_empty_packet(self, clock_time_ms: int = 80) -> bytes:
        """Create an empty packet containing only header/ack metadata."""
        send_buffer = self.init_send_buffer(clock_time_ms)
        return self._finalize_send_buffer(send_buffer)

    def _finalize_send_buffer(self, writer: FBitWriter) -> bytes:
        """Apply trailing 1-bits and handler Outgoing, return raw bytes."""
        writer.write_bit(1)  # FlushNet trailing 1-bit (inner, always)
        for handler in self.handler_components:
            writer = handler.Outgoing(writer)
        if self.handler_components:
            writer.write_bit(1)  # Outgoing_Internal trailing 1-bit (outer, only when handlers exist)
        return writer.get_buffer()

    def create_channel_by_name(self, ch_name=None, ch_index=None) -> Channel:
        if ch_index is None:
            try:
                ch_index = self.channels.index(None)
            except ValueError:
                raise RuntimeError("No free channel slots left")

        if not self._is_valid_channel_index(ch_index):
            raise ValueError(f"Channel index {ch_index} out of bounds")

        if self.channels[ch_index] is not None:
            raise RuntimeError(f"Channel index {ch_index} already in use")

        if ch_name is None:
            raise ValueError("Channel name is required")

        channel = create_channel(self, ch_name, ch_index)

        self.channels[ch_index] = channel
        return channel

    def _handle_ack(self, seq: SequenceNumber, delivered: bool):
        self.out_ack_packet_id = seq.value

        ch_indices = self.channel_record.pop(seq.value, None)
        if ch_indices is None:
            return

        seen = set()
        for ch_index in ch_indices:
            if ch_index in seen:
                continue
            seen.add(ch_index)

            channel = self.channels[ch_index]
            if channel is None:
                continue

            if delivered:
                channel.received_ack(seq.value)
            else:
                self.resend_packets.extend(channel.received_nak(seq.value))

    def _parse_packet_header_and_update_notify(self, reader: FBitReader) -> bool:
        notification_header = self.packet_notify.read_header(reader)
        if notification_header is None:
            return False

        # v14+ (JitterInHeader): optional packet info payload
        if self.engine_net_ver >= ENGINE_NET_VER_JITTER_IN_HEADER:
            b_has_packet_info = reader.read_bit()
            if b_has_packet_info:
                reader.read_int(MAX_JITTER_CLOCK_TIME_VALUE + 1)
                b_has_server_frame_time = reader.read_bit()
                if b_has_server_frame_time:
                    reader.read_uint8()

        seq_delta = self.packet_notify.update(notification_header, self._handle_ack)

        if seq_delta > 0:
            self.in_packet_id = notification_header.seq.value
            return True
        return False

    def _parse_bunch_header(self, reader: FBitReader) -> FInBunch:
        bunch = FInBunch()
        bunch.PacketId = self.in_packet_id

        is_control = reader.read_bit()
        bunch.bOpen = is_control and reader.read_bit()
        bunch.bClose = is_control and reader.read_bit()
        # v7+ (ChannelCloseReason): full close reason enum
        # Older: single bDormant bit
        if bunch.bClose:
            if self.engine_net_ver >= ENGINE_NET_VER_CHANNEL_CLOSE_REASON:
                bunch.CloseReason = reader.read_int(CLOSE_REASON_MAX)
            else:
                b_dormant = reader.read_bit()
                bunch.CloseReason = EChannelCloseReason.Dormancy if b_dormant else EChannelCloseReason.Destroyed
        else:
            bunch.CloseReason = EChannelCloseReason.Destroyed
        bunch.bIsReplicationPaused = reader.read_bit()
        bunch.bReliable = reader.read_bit()
        bunch.ChIndex = reader.read_uint32_packed()
        if not self._is_valid_channel_index(bunch.ChIndex):
            raise ValueError(f"Incoming bunch channel index out of bounds: {bunch.ChIndex}")
        bunch.bHasPackageMapExports = reader.read_bit()
        bunch.bHasMustBeMappedGUIDs = reader.read_bit()
        bunch.bPartial = reader.read_bit()

        if bunch.bReliable:
            if self.is_internal_ack():
                bunch.ChSequence = (self.in_reliable[bunch.ChIndex] + 1) & MAX_CHSEQUENCE_MASK
            else:
                value = reader.read_int(MAX_CHSEQUENCE)
                reference = self.in_reliable[bunch.ChIndex]
                make_relative = ((value - reference + MAX_CHSEQUENCE_HALF) & MAX_CHSEQUENCE_MASK) - MAX_CHSEQUENCE_HALF
                bunch.ChSequence = (reference + make_relative) & MAX_CHSEQUENCE_MASK
        elif bunch.bPartial:
            bunch.ChSequence = self.in_packet_id
        else:
            bunch.ChSequence = 0

        if bunch.bPartial:
            bunch.bPartialInitial = reader.read_bit()
            bunch.bPartialCustomExportsFinal = reader.read_bit()
            bunch.bPartialFinal = reader.read_bit()

        self.total_bunches_received += 1
        if bunch.bHasPackageMapExports:
            self.bunches_with_exports += 1

        if bunch.bOpen or bunch.bReliable:
            bHardcoded = reader.read_bit()
            if bHardcoded:
                bunch.ChNameIndex = reader.read_uint32_packed()
            else:
                inString = reader.read_fstring()
                inNumber = reader.read_uint32()
                bunch.ChNameIndex = FName(inString, inNumber).index

            try:
                bunch.ChName = EName(bunch.ChNameIndex)
            except ValueError:
                bunch.ChName = EName.None_

            bunch.ChType = get_channel_type(bunch.ChName)
        else:
            bunch.ChType = get_channel_type(EName.None_)
            bunch.ChName = EName.None_

        return bunch

    def _should_skip_duplicate_reliable_bunch(self, reader: FBitReader, bunch: FInBunch) -> bool:
        if not bunch.bReliable or bunch.ChSequence > self.in_reliable[bunch.ChIndex]:
            return False

        skip_bits = reader.read_int(self.max_bunch_payload_bits)
        if skip_bits > reader.get_bits_left():
            raise ValueError(
                f"Reliable bunch skip overflow: wanted {skip_bits}, left {reader.get_bits_left()}"
            )
        reader.skip_bits(skip_bits)
        return True

    def _get_existing_channel_checked(self, bunch: FInBunch) -> Optional[Channel]:
        channel = self.channels[bunch.ChIndex]
        if channel is not None and bunch.ChName != EName.None_:
            if channel.ch_name != EName.None_ and channel.ch_name != bunch.ChName:
                raise RuntimeError(f"Channel type mismatch: {channel.ch_name} != {bunch.ChName}")
        return channel

    def _read_bunch_payload_reader(self, reader: FBitReader) -> FBitReader:
        bunch_data_bits = reader.read_int(self.max_bunch_payload_bits)
        if bunch_data_bits > reader.get_bits_left():
            raise ValueError(
                f"Bunch payload overflow: wanted {bunch_data_bits}, left {reader.get_bits_left()}"
            )
        bunch_data = reader.serialize_bits(bunch_data_bits)
        return FBitReader(bunch_data, num_bits=bunch_data_bits)

    def _read_bunch_payload(self, reader: FBitReader, bunch: FInBunch) -> None:
        bunch_reader = self._read_bunch_payload_reader(reader)
        total_bits = bunch_reader.get_bits_left()
        if total_bits > 0:
            data = bunch_reader.serialize_bits(total_bits)
            bunch.set_data(data, total_bits)
        else:
            bunch.set_data(b'', 0)

    def _resolve_channel_for_bunch(
        self,
        bunch: FInBunch,
        channel: Optional[Channel],
    ) -> Optional[Channel]:
        if channel is None:
            if bunch.ChName == EName.None_:
                return None
            channel = self.create_channel_by_name(ch_name=bunch.ChName, ch_index=bunch.ChIndex)
        return channel

    def _dispatch_bunch(self, bunch: FInBunch, channel: Channel, final_packets: list[bytes]) -> bool:
        b_deleted, b_skip_ack = channel.received_raw_bunch(bunch)
        final_packets.extend(channel.final_packets)
        channel.final_packets.clear()

        if b_deleted:
            if bunch.ChIndex == 0:
                self.b_closed = True
                print(f"[Connection] Server closed control channel (reason={self.close_reason or 'unknown'})")
            self.channels[bunch.ChIndex] = None

        return b_skip_ack or bunch.is_error()

    def _process_single_bunch(self, reader: FBitReader, final_packets: list[bytes]) -> bool:
        bunch = self._parse_bunch_header(reader)

        if self._should_skip_duplicate_reliable_bunch(reader, bunch):
            return False

        channel = self._get_existing_channel_checked(bunch)
        self._read_bunch_payload(reader, bunch)
        channel = self._resolve_channel_for_bunch(bunch, channel)
        if channel is None:
            return False

        return self._dispatch_bunch(bunch, channel, final_packets)

    def _process_bunches(self, reader: FBitReader, final_packets: list[bytes]) -> bool:
        b_skip_ack = False
        while not reader.at_end() and reader.get_bits_left() >= MIN_BUNCH_HEADER_BITS:
            try:
                b_skip_ack = b_skip_ack or self._process_single_bunch(reader, final_packets)

            except PARSE_EXCEPTIONS:
                report_exception("NetConnection bunch processing failed")
                b_skip_ack = True
                break

        return b_skip_ack

    def received_packet(self, reader: FBitReader) -> list[bytes]:
        final_packets = []

        if not self._parse_packet_header_and_update_notify(reader):
            return final_packets

        if reader.get_bits_left() < MIN_BUNCH_HEADER_BITS:
            self.packet_notify.ack_seq(SequenceNumber(self.in_packet_id))
            final_packets.append(self.create_empty_packet(80))
            return final_packets

        if self.resend_packets:
            final_packets.extend(self.resend_packets)
            self.resend_packets.clear()

        b_skip_ack = self._process_bunches(reader, final_packets)

        # Keep sequence history in sync even when rejecting packet payload.
        if b_skip_ack:
            self.packet_notify.nak_seq(SequenceNumber(self.in_packet_id))
        else:
            self.packet_notify.ack_seq(SequenceNumber(self.in_packet_id))

        return final_packets

    def _create_ack_packet(self) -> bytes:
        """Create ACK-only packet (no bunches, just header with ACK info)."""
        return self.create_empty_packet(80)

    def received_raw_packet(self, data: bytes) -> list[bytes]:
        if len(data) < 1:
            return []

        if self.handler_components:
            # Strip Outgoing_Internal trailing 1-bit (outer, only when handlers exist)
            bit_size = FBitUtil.strip_trailing_one(data)
            if bit_size <= 0:
                return []

            reader = FBitReader(data, num_bits=bit_size)

            try:
                for handler in reversed(self.handler_components):
                    reader = handler.Incoming(reader)
            except PARSE_EXCEPTIONS:
                report_exception("NetConnection incoming handler processing failed")
                return []

            # Extract processed data for FlushNet bit stripping
            remaining = reader.get_bits_left()
            if remaining <= 0:
                return []
            data = reader.serialize_bits(remaining)

        # Strip FlushNet trailing 1-bit (inner, always)
        bit_size = FBitUtil.strip_trailing_one(data)
        if bit_size <= 0:
            return []

        reader = FBitReader(data, num_bits=bit_size)

        if reader.get_bits_left() > 0:
            with use_package_map_state(self.package_map_state):
                return self.received_packet(reader)

        return []
