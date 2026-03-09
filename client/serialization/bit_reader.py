# serialization/bit_reader.py
from __future__ import annotations
import struct
from serialization.bit_util import FBitUtil


class BitReaderError(RuntimeError):
    pass


class FBitReader:
    __slots__ = ('_pos', '_buffer', '_num', '_error')

    def __init__(self, src: bytes | bytearray | None = None, num_bits: int | None = None) -> None:
        self._pos: int = 0
        self._buffer: bytearray = bytearray(src or b"")
        self._num: int = num_bits if num_bits is not None else len(self._buffer) * 8
        self._error: bool = False
        self._apply_mask()

    def _apply_mask(self) -> None:
        extra_bits = self._num & 7
        if extra_bits:
            byte_idx = self._num >> 3
            mask = FBitUtil.G_MASK[extra_bits]
            if byte_idx < len(self._buffer):
                self._buffer[byte_idx] &= mask
            else:
                self._buffer.append(0)

    def _ensure_bits(self, length_bits: int) -> None:
        if self._pos + length_bits > self._num:
            raise BitReaderError(f"BitReader overflow - requested {length_bits} bits, {self.get_bits_left()} remain")

    def set_error(self) -> None:
        self._error = True

    def is_error(self) -> bool:
        return self._error

    def read_bit(self) -> bool:
        if self._pos >= self._num:
            raise BitReaderError("BitReader overflow - requested 1 bits, 0 remain")
        bit = (self._buffer[self._pos >> 3] >> (self._pos & 7)) & 1
        self._pos += 1
        return bool(bit)

    def read_byte(self) -> int:
        pos = self._pos
        if pos + 8 <= self._num and (pos & 7) == 0:
            self._pos = pos + 8
            return self._buffer[pos >> 3]
        return self.serialize_bits(8)[0]

    def read_double(self) -> float:
        pos = self._pos
        if pos + 64 <= self._num and (pos & 7) == 0:
            self._pos = pos + 64
            return struct.unpack_from("<d", self._buffer, pos >> 3)[0]
        return struct.unpack("<d", self.serialize_bits(64))[0]

    def read_fstring(self) -> str:
        length = self.read_int32()
        load_ucs2 = length < 0
        if load_ucs2:
            length = -length

        if length == 0:
            return ""
        if length == 1:
            self.serialize_bits(16 if load_ucs2 else 8)
            return ""

        if load_ucs2:
            raw = self.serialize_bits(length * 16)
            return raw[:-2].decode("utf-16-le")
        else:
            raw = self.serialize_bits(length * 8)
            return raw[:-1].decode("utf-8")

    def serialize_bits(self, length_bits: int) -> bytes:
        if length_bits == 0:
            return b""

        if self._pos + length_bits > self._num:
            raise BitReaderError(f"BitReader overflow - requested {length_bits} bits, {self.get_bits_left()} remain")

        if length_bits == 1:
            bit = (self._buffer[self._pos >> 3] >> (self._pos & 7)) & 1
            self._pos += 1
            return bytes((bit,))

        # Fast path: byte-aligned position
        if (self._pos & 7) == 0:
            byte_pos = self._pos >> 3
            num_bytes = (length_bits + 7) >> 3
            self._pos += length_bits
            tail = length_bits & 7
            if tail:
                result = bytearray(self._buffer[byte_pos:byte_pos + num_bytes])
                result[-1] &= (1 << tail) - 1
                return bytes(result)
            return bytes(self._buffer[byte_pos:byte_pos + num_bytes])

        dest = bytearray((length_bits + 7) >> 3)
        FBitUtil.app_bits_cpy(dest, 0, self._buffer, self._pos, length_bits)
        self._pos += length_bits
        return bytes(dest)

    def serialize(self, length_bytes: int) -> bytes:
        return self.serialize_bits(length_bytes * 8)

    def read_int(self, value_max: int) -> int:
        if value_max < 2:
            raise ValueError("value_max must be >= 2")

        value = 0
        mask = 1
        buf = self._buffer
        pos = self._pos
        num = self._num
        while (value + mask) < value_max:
            if pos >= num:
                raise BitReaderError("SerializeInt overflow")
            if (buf[pos >> 3] >> (pos & 7)) & 1:
                value |= mask
            mask <<= 1
            pos += 1
        self._pos = pos
        return value

    def read_int_wrapped(self, value_max: int) -> int:
        from math import ceil, log2
        if value_max < 2:
            raise ValueError("value_max must be >= 2")

        length_bits = int(ceil(log2(value_max)))
        raw = self.serialize_bits(length_bits)
        return int.from_bytes(raw, "little") & ((1 << length_bits) - 1)

    def read_int8(self) -> int:
        pos = self._pos
        if pos + 8 <= self._num and (pos & 7) == 0:
            self._pos = pos + 8
            return struct.unpack_from("<b", self._buffer, pos >> 3)[0]
        return struct.unpack("<b", self.serialize_bits(8))[0]

    def read_int16(self) -> int:
        pos = self._pos
        if pos + 16 <= self._num and (pos & 7) == 0:
            self._pos = pos + 16
            return struct.unpack_from("<h", self._buffer, pos >> 3)[0]
        return struct.unpack("<h", self.serialize_bits(16))[0]

    def read_int32(self) -> int:
        pos = self._pos
        if pos + 32 <= self._num and (pos & 7) == 0:
            self._pos = pos + 32
            return struct.unpack_from("<i", self._buffer, pos >> 3)[0]
        return struct.unpack("<i", self.serialize_bits(32))[0]

    def read_float(self) -> float:
        pos = self._pos
        if pos + 32 <= self._num and (pos & 7) == 0:
            self._pos = pos + 32
            return struct.unpack_from("<f", self._buffer, pos >> 3)[0]
        return struct.unpack("<f", self.serialize_bits(32))[0]

    def read_uint16(self) -> int:
        pos = self._pos
        if pos + 16 <= self._num and (pos & 7) == 0:
            self._pos = pos + 16
            return struct.unpack_from("<H", self._buffer, pos >> 3)[0]
        return struct.unpack("<H", self.serialize_bits(16))[0]

    def read_uint32(self) -> int:
        pos = self._pos
        if pos + 32 <= self._num and (pos & 7) == 0:
            self._pos = pos + 32
            return struct.unpack_from("<I", self._buffer, pos >> 3)[0]
        return struct.unpack("<I", self.serialize_bits(32))[0]

    def read_uint64(self) -> int:
        pos = self._pos
        if pos + 64 <= self._num and (pos & 7) == 0:
            self._pos = pos + 64
            return struct.unpack_from("<Q", self._buffer, pos >> 3)[0]
        return struct.unpack("<Q", self.serialize_bits(64))[0]

    def read_uint32_packed(self) -> int:
        buf = self._buffer
        pos = self._pos
        num = self._num
        bit_used = pos & 7
        result = 0

        if bit_used == 0:
            for shift in range(0, 35, 7):
                if pos + 8 > num:
                    raise BitReaderError("SerializeIntPacked overflow")
                byte_val = buf[pos >> 3]
                pos += 8
                result |= (byte_val >> 1) << shift
                if not (byte_val & 1):
                    break
            else:
                raise BitReaderError("SerializeIntPacked: continuation bit still set after 5 bytes")
        else:
            bit_left = 8 - bit_used
            mask0 = (1 << bit_left) - 1
            mask1 = (1 << bit_used) - 1
            for shift in range(0, 35, 7):
                if pos + 8 > num:
                    raise BitReaderError("SerializeIntPacked overflow")
                src_idx = pos >> 3
                byte_val = ((buf[src_idx] >> bit_used) & mask0) | ((buf[src_idx + 1] & mask1) << bit_left)
                pos += 8
                result |= (byte_val >> 1) << shift
                if not (byte_val & 1):
                    break
            else:
                raise BitReaderError("SerializeIntPacked: continuation bit still set after 5 bytes")

        self._pos = pos
        return result

    def read_uint64_packed(self) -> int:
        buf = self._buffer
        pos = self._pos
        num = self._num
        bit_used = pos & 7
        result = 0

        if bit_used == 0:
            for shift in range(0, 70, 7):
                if pos + 8 > num:
                    break
                byte_val = buf[pos >> 3]
                pos += 8
                result |= (byte_val >> 1) << shift
                if not (byte_val & 1):
                    break
        else:
            bit_left = 8 - bit_used
            mask0 = (1 << bit_left) - 1
            mask1 = (1 << bit_used) - 1
            for shift in range(0, 70, 7):
                if pos + 8 > num:
                    break
                src_idx = pos >> 3
                byte_val = ((buf[src_idx] >> bit_used) & mask0) | ((buf[src_idx + 1] & mask1) << bit_left)
                pos += 8
                result |= (byte_val >> 1) << shift
                if not (byte_val & 1):
                    break

        self._pos = pos
        return result

    @property
    def num_bits(self) -> int:
        return self._num

    def get_buffer(self) -> bytes:
        return bytes(self._buffer[:(self._num + 7) >> 3])

    def get_num_bits(self) -> int:
        return self._num

    def get_num_bytes(self) -> int:
        return (self._num + 7) >> 3

    def get_pos_bits(self) -> int:
        return self._pos

    def set_pos_bits(self, pos: int) -> None:
        if pos < 0 or pos > self._num:
            raise BitReaderError(f"BitReader position out of range: {pos}")
        self._pos = pos

    def skip_bits(self, count: int) -> None:
        new_pos = self._pos + count
        if count < 0 or new_pos < 0 or new_pos > self._num:
            raise BitReaderError(f"BitReader skip out of range: pos={self._pos}, count={count}")
        self._pos = new_pos

    def eat_byte_align(self) -> None:
        new_pos = (self._pos + 7) & ~0x07
        if new_pos > self._num:
            raise BitReaderError("FBitReader EatByteAlign overflow")
        self._pos = new_pos

    def get_bits_left(self) -> int:
        return max(self._num - self._pos, 0)

    def get_bytes_left(self) -> int:
        return (self._num - self._pos + 7) >> 3

    def at_end(self) -> bool:
        return self._pos >= self._num

    def get_buffer_pos_checked(self) -> int:
        if self._pos & 7:
            raise BitReaderError("FBitReader Pos % 8 != 0")
        return self._pos >> 3

    def reset_data(self) -> None:
        self._buffer = bytearray()
        self._num = 0
        self._pos = 0
        self._error = False

    def append_data_from_checked(self, in_buffer_pos: int, in_buffer: bytes, in_bits_count: int) -> None:
        if self._num & 7:
            raise BitReaderError("FBitReader Pos % 8 != 0")

        if in_buffer_pos < 0 or in_buffer_pos > len(in_buffer):
            raise ValueError("in_buffer_pos out of range")

        existing_bytes = (self._num + 7) >> 3
        tail_bytes = len(in_buffer) - in_buffer_pos

        merged = bytearray(existing_bytes + tail_bytes)
        if existing_bytes:
            merged[:existing_bytes] = self._buffer[:existing_bytes]
        if tail_bytes:
            merged[existing_bytes:] = in_buffer[in_buffer_pos:]

        self._buffer = merged
        self._num += in_bits_count
        self._apply_mask()
