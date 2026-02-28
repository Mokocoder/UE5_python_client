# serialization/bit_util.py
from __future__ import annotations

G_SHIFT = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80)
G_MASK = (0x00, 0x01, 0x03, 0x07, 0x0F, 0x1F, 0x3F, 0x7F)


class FBitUtil:
    G_SHIFT = G_SHIFT
    G_MASK = G_MASK

    @staticmethod
    def strip_trailing_one(data: bytes) -> int:
        """Find actual bit length by stripping the trailing 1-bit terminator"""
        if not data:
            return 0
        last_byte = data[-1]
        if last_byte == 0:
            return 0
        bit_size = len(data) * 8 - 1
        while not (last_byte & 0x80):
            last_byte <<= 1
            bit_size -= 1
        return bit_size

    @staticmethod
    def app_bits_cpy(dest: bytearray, dest_bit: int, src: bytes | bytearray,
                     src_bit: int, bit_count: int) -> None:
        if bit_count <= 0:
            return

        # Small copy fast path
        if bit_count <= 8:
            s_byte = src_bit >> 3
            s_off = src_bit & 7
            if s_off + bit_count <= 8:
                accu = src[s_byte] >> s_off
            else:
                accu = (src[s_byte] >> s_off) | (src[s_byte + 1] << (8 - s_off))
            accu &= (1 << bit_count) - 1

            d_byte = dest_bit >> 3
            d_off = dest_bit & 7
            write_mask_lo = ((1 << bit_count) - 1) << d_off
            dest[d_byte] = (dest[d_byte] & ((write_mask_lo & 0xFF) ^ 0xFF)) | ((accu << d_off) & 0xFF)
            if d_off + bit_count > 8:
                write_mask_hi = (write_mask_lo >> 8) & 0xFF
                dest[d_byte + 1] = (dest[d_byte + 1] & (write_mask_hi ^ 0xFF)) | ((accu >> (8 - d_off)) & 0xFF)
            return

        d_off = dest_bit & 7
        s_off = src_bit & 7

        if d_off == 0 and s_off == 0:
            d_byte = dest_bit >> 3
            s_byte = src_bit >> 3
            full = bit_count >> 3
            tail = bit_count & 7
            if full > 0:
                dest[d_byte:d_byte + full] = src[s_byte:s_byte + full]
            if tail > 0:
                mask = (1 << tail) - 1
                d = d_byte + full
                dest[d] = (dest[d] & ~mask) | (src[s_byte + full] & mask)
            return

        d_base = dest_bit >> 3
        s_base = src_bit >> 3
        full = bit_count >> 3

        if d_off == 0:
            s_hi = 8 - s_off
            for j in range(full):
                sb = s_base + j
                dest[d_base + j] = ((src[sb] >> s_off) | (src[sb + 1] << s_hi)) & 0xFF
        elif s_off == 0:
            d_lo = (1 << d_off) - 1
            d_hi = ~d_lo & 0xFF
            d_shift = 8 - d_off
            for j in range(full):
                val = src[s_base + j]
                db = d_base + j
                dest[db] = (dest[db] & d_lo) | ((val << d_off) & 0xFF)
                dest[db + 1] = (dest[db + 1] & d_hi) | (val >> d_shift)
        else:
            s_hi = 8 - s_off
            d_lo = (1 << d_off) - 1
            d_hi = ~d_lo & 0xFF
            d_shift = 8 - d_off
            for j in range(full):
                sb = s_base + j
                val = ((src[sb] >> s_off) | (src[sb + 1] << s_hi)) & 0xFF
                db = d_base + j
                dest[db] = (dest[db] & d_lo) | ((val << d_off) & 0xFF)
                dest[db + 1] = (dest[db + 1] & d_hi) | (val >> d_shift)

        for i in range(full << 3, bit_count):
            s_idx = (src_bit + i) >> 3
            bit_val = (src[s_idx] >> ((src_bit + i) & 7)) & 1
            d_idx = (dest_bit + i) >> 3
            d_b = (dest_bit + i) & 7
            if bit_val:
                dest[d_idx] |= (1 << d_b)
            else:
                dest[d_idx] &= ~(1 << d_b) & 0xFF
