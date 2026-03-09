# net/channels/base_channel.py
"""Base Channel with partial bunch handling."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Optional

from core.names.ename import EName
from net.packets.in_bunch import FInBunch
from net.packet_id_range import FPacketIdRange
from net.channels.channel_types import EChannelCloseReason
from net.guid.package_map_client import PackageMapClient
from constants import NET_MAX_CONSTRUCTED_PARTIAL_BUNCH_SIZE_BYTES, RELIABLE_BUFFER

if TYPE_CHECKING:
    from net.connection import NetConnection
    from net.packets.out_bunch import FOutBunch


class Channel:
    __slots__ = (
        'connection', 'ch_index', 'ch_name', 'created_at',
        'opened_locally', 'open_acked', 'closing', 'dormant', 'broken',
        'open_packet_id',
        'num_in_rec', 'in_rec',
        'in_partial_bunch',
        'out_rec', 'num_out_rec',
        'final_packets',
    )

    def __init__(self, connection: NetConnection = None, ch_index: int = -1, ch_name: EName = None):
        self.connection = connection
        self.ch_index = ch_index
        self.ch_name = ch_name
        self.created_at: float = time.perf_counter()

        self.opened_locally: bool = ch_name == EName.Control
        self.open_acked: bool = not self.opened_locally
        self.closing: bool = False
        self.dormant: bool = False
        self.broken: bool = False

        self.open_packet_id = FPacketIdRange(-1, -1)

        self.num_in_rec: int = 0
        self.in_rec: dict[int, FInBunch] = {}

        self.in_partial_bunch: Optional[FInBunch] = None

        self.out_rec: list[FOutBunch] = []
        self.num_out_rec: int = 0

        self.final_packets: list[bytes] = []

    def received_raw_bunch(self, bunch: FInBunch) -> tuple[bool, bool]:
        b_skip_ack = False

        if bunch.bHasPackageMapExports:
            self._receive_net_guid_bunch(bunch)
            if bunch.is_error():
                return False, False

        if self.broken:
            return False, False

        if bunch.bReliable and bunch.ChSequence != self.connection.in_reliable[self.ch_index] + 1:
            if bunch.ChSequence in self.in_rec:
                return False, b_skip_ack

            if bunch.bPartialCustomExportsFinal:
                b_skip_ack = True

            self.in_rec[bunch.ChSequence] = FInBunch(bunch=bunch, copy_buffer=True)
            self.num_in_rec += 1

            if self.num_in_rec >= RELIABLE_BUFFER:
                bunch.set_error()
                return False, b_skip_ack

            return False, b_skip_ack

        b_deleted, b_local_skip = self.received_next_bunch(bunch)

        if bunch.is_error():
            return False, b_skip_ack

        if b_deleted:
            return True, b_skip_ack

        b_skip_ack = b_skip_ack or b_local_skip

        while self.in_rec:
            expected = self.connection.in_reliable[self.ch_index] + 1
            if expected not in self.in_rec:
                break

            release = self.in_rec.pop(expected)
            self.num_in_rec -= 1

            b_deleted, b_local_skip2 = self.received_next_bunch(release)

            if release.is_error():
                return False, b_skip_ack

            if b_deleted:
                return True, b_skip_ack

        return False, b_skip_ack

    def received_next_bunch(self, bunch: FInBunch) -> tuple[bool, bool]:
        b_skip_ack = False

        if bunch.bReliable:
            self.connection.in_reliable[bunch.ChIndex] = bunch.ChSequence

        handle_bunch: Optional[FInBunch] = bunch

        if bunch.bPartial:
            handle_bunch = None

            if bunch.bPartialInitial:
                if self.in_partial_bunch is not None:
                    if not self.in_partial_bunch.bPartialFinal:
                        if self.in_partial_bunch.bReliable:
                            if bunch.bReliable:
                                bunch.set_error()
                                return False, b_skip_ack
                            b_skip_ack = True
                            return False, b_skip_ack
                    self.in_partial_bunch = None

                self.in_partial_bunch = FInBunch(bunch=bunch, copy_buffer=False)

                if not bunch.bHasPackageMapExports and bunch.get_bits_left() > 0:
                    if (not bunch.bPartialCustomExportsFinal
                            and bunch.get_bits_left() % 8 != 0):
                        bunch.set_error()
                        return False, b_skip_ack

                    self.in_partial_bunch.append_data_from_checked(
                        bunch.get_buffer_pos_checked(),
                        bunch.get_buffer(),
                        bunch.get_bits_left()
                    )
            else:
                b_sequence_matches = False

                if self.in_partial_bunch is not None:
                    b_reliable_matches = bunch.ChSequence == self.in_partial_bunch.ChSequence + 1
                    b_unreliable_matches = b_reliable_matches or bunch.ChSequence == self.in_partial_bunch.ChSequence
                    b_sequence_matches = b_reliable_matches if self.in_partial_bunch.bReliable else b_unreliable_matches

                if (self.in_partial_bunch is not None and not self.in_partial_bunch.bPartialFinal and b_sequence_matches and self.in_partial_bunch.bReliable == bunch.bReliable):
                    # Merge data
                    if not bunch.bHasPackageMapExports and bunch.get_bits_left() > 0:
                        self.in_partial_bunch.append_data_from_checked(
                            bunch.get_buffer_pos_checked(),
                            bunch.get_buffer(),
                            bunch.get_bits_left(),
                        )

                    # Byte alignment check (non-final, non-custom-export)
                    if (not bunch.bHasPackageMapExports and not bunch.bPartialCustomExportsFinal and not bunch.bPartialFinal and bunch.get_bits_left() % 8 != 0):
                        bunch.set_error()
                        return False, b_skip_ack

                    self.in_partial_bunch.ChSequence = bunch.ChSequence

                    if bunch.bPartialFinal:
                        if bunch.bHasPackageMapExports:
                            bunch.set_error()
                            return False, b_skip_ack

                        handle_bunch = self.in_partial_bunch
                        self.in_partial_bunch.bPartialFinal = True
                        self.in_partial_bunch.bClose = bunch.bClose
                        self.in_partial_bunch.CloseReason = bunch.CloseReason
                        self.in_partial_bunch.bIsReplicationPaused = bunch.bIsReplicationPaused
                        self.in_partial_bunch.bHasMustBeMappedGUIDs = bunch.bHasMustBeMappedGUIDs
                else:
                    # Merge failure
                    b_skip_ack = True

                    if (self.in_partial_bunch is not None and self.in_partial_bunch.bReliable):
                        if bunch.bReliable:
                            bunch.set_error()
                            return False, b_skip_ack
                        return False, b_skip_ack

                    self.in_partial_bunch = None

            if self.in_partial_bunch is not None:
                if self.in_partial_bunch.get_num_bytes() > NET_MAX_CONSTRUCTED_PARTIAL_BUNCH_SIZE_BYTES:
                    bunch.set_error()
                    return False, b_skip_ack

            if (not b_skip_ack and bunch.bPartialCustomExportsFinal and self.in_partial_bunch is not None):
                self.in_partial_bunch.bPartialCustomExportsFinal = bunch.bPartialCustomExportsFinal
                self._receive_custom_exports_bunch(self.in_partial_bunch)
                if self.in_partial_bunch.is_error():
                    return False, b_skip_ack
                self.in_partial_bunch.reset_data()
                self.in_partial_bunch.bPartialCustomExportsFinal = False

        if handle_bunch is not None:
            b_both_sides_can_open = self.is_both_sides_can_open()

            if handle_bunch.bOpen:
                if not b_both_sides_can_open:
                    if self.opened_locally:
                        bunch.set_error()
                        return False, b_skip_ack
                    if self.open_packet_id.first != -1 or self.open_packet_id.last != -1:
                        bunch.set_error()
                        return False, b_skip_ack

                self.open_packet_id = FPacketIdRange(handle_bunch.PacketId, bunch.PacketId)
                self.open_acked = True
                if handle_bunch.ExportNetGUIDs:
                    self.on_open_guids(handle_bunch.ExportNetGUIDs)

            if not b_both_sides_can_open:
                if not self.opened_locally and not self.open_acked:
                    if handle_bunch.bReliable:
                        bunch.set_error()
                        return False, b_skip_ack
                    b_skip_ack = True
                    return False, b_skip_ack

            b_deleted = self.received_sequenced_bunch(handle_bunch)
            return b_deleted, b_skip_ack

        return False, b_skip_ack

    def received_sequenced_bunch(self, bunch: FInBunch) -> bool:
        if not self.closing:
            self.received_bunch(self.connection, bunch)

        if bunch.bClose:
            self.dormant = (bunch.CloseReason == EChannelCloseReason.Dormancy)
            self.on_channel_closed(bunch.CloseReason)
            return True

        return False

    def received_bunch(self, connection: NetConnection, bunch: FInBunch):
        raise NotImplementedError("Subclass must implement received_bunch")

    def is_both_sides_can_open(self) -> bool:
        from net.channels.channel_registry import get_registration
        reg = get_registration(self.ch_name)
        if reg is None:
            return False
        return reg.b_server_open and reg.b_client_open

    def on_channel_closed(self, close_reason: EChannelCloseReason) -> None:
        pass

    def add_out_rec(self, bunch: FOutBunch) -> None:
        self.out_rec.append(bunch)
        self.num_out_rec += 1

    def received_ack(self, acked_packet_id: int) -> None:
        for out in self.out_rec:
            if out.PacketId == acked_packet_id:
                out.ReceivedAck = True
        self._clean_acked_out_rec()

    def received_nak(self, nak_packet_id: int) -> list[bytes]:
        resend_packets: list[bytes] = []
        for out in self.out_rec:
            if out.PacketId == nak_packet_id and not out.ReceivedAck:
                send_buffer = self.connection.init_send_buffer()
                out.PacketId = self.connection.out_packet_id
                self.connection.channel_record.setdefault(out.PacketId, []).append(self.ch_index)
                resend_packets.append(self.connection.write_bunch_to_send_buffer(out, send_buffer))
                print(f"[RESEND] Ch {self.ch_index} resending ChSeq {out.ChSequence}")
        return resend_packets

    def _clean_acked_out_rec(self) -> None:
        while self.out_rec and self.out_rec[0].ReceivedAck:
            self.out_rec.pop(0)
            self.num_out_rec -= 1

    def _receive_net_guid_bunch(self, bunch: FInBunch) -> None:
        PackageMapClient.begin_bunch_guids()
        has_rep_layout_export = bunch.read_bit()
        if has_rep_layout_export:
            PackageMapClient.ReceiveNetFieldExportsCompat(bunch)
        else:
            num_guids = bunch.read_int32()
            for _ in range(num_guids):
                PackageMapClient.InternalLoadObject(bunch, True)
        bunch.ExportNetGUIDs = PackageMapClient.get_bunch_guids()
        remaining_bits = bunch.get_bits_left()
        if remaining_bits > 0:
            remaining = bunch.serialize_bits(remaining_bits)
            bunch.set_data(remaining, remaining_bits)
        else:
            bunch.set_data(b'', 0)

    def _receive_custom_exports_bunch(self, bunch: FInBunch) -> None:
        pass

    def on_open_guids(self, guids: list[int]) -> None:
        pass
