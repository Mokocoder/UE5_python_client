# net/reliability/sequence_number.py
"""Wrapping sequence number (TSequenceNumber<SEQUENCE_NUMBER_BITS>)."""
from __future__ import annotations

from constants import SEQUENCE_NUMBER_BITS, SEQ_NUMBER_COUNT, SEQ_NUMBER_HALF, SEQ_NUMBER_MASK


class SequenceNumber:
    __slots__ = ('_value',)

    NumBits = SEQUENCE_NUMBER_BITS
    SeqNumberCount = SEQ_NUMBER_COUNT
    SeqNumberHalf = SEQ_NUMBER_HALF
    SeqNumberMax = SEQ_NUMBER_COUNT - 1
    SeqNumberMask = SEQ_NUMBER_MASK

    def __init__(self, value: int = 0):
        self._value: int = value & SequenceNumber.SeqNumberMask

    @property
    def value(self) -> int:
        return self._value

    def get(self) -> int:
        return self._value

    def greater(self, other: SequenceNumber) -> bool:
        return (
            self._value != other._value
            and ((self._value - other._value) & SequenceNumber.SeqNumberMask) < SequenceNumber.SeqNumberHalf
        )

    def greater_eq(self, other: SequenceNumber) -> bool:
        return ((self._value - other._value) & SequenceNumber.SeqNumberMask) < SequenceNumber.SeqNumberHalf

    def __gt__(self, other: SequenceNumber) -> bool:
        return self.greater(other)

    def __ge__(self, other: SequenceNumber) -> bool:
        return self.greater_eq(other)

    def __lt__(self, other: SequenceNumber) -> bool:
        return other.greater(self)

    def __le__(self, other: SequenceNumber) -> bool:
        return other.greater_eq(self)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SequenceNumber):
            return self._value == other._value
        if isinstance(other, int):
            return self._value == (other & SequenceNumber.SeqNumberMask)
        return False

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(self._value)

    @staticmethod
    def diff(a: SequenceNumber, b: SequenceNumber) -> int:
        raw = (a._value - b._value) & SequenceNumber.SeqNumberMask
        if raw >= SequenceNumber.SeqNumberHalf:
            raw -= SequenceNumber.SeqNumberCount
        return raw

    def increment_and_get(self) -> SequenceNumber:
        return SequenceNumber(self._value + 1)

    def __iadd__(self, other: int) -> SequenceNumber:
        self._value = (self._value + other) & SequenceNumber.SeqNumberMask
        return self

    def __add__(self, other: int) -> SequenceNumber:
        return SequenceNumber(self._value + other)

    def __sub__(self, other: SequenceNumber) -> SequenceNumber:
        return SequenceNumber(self._value - other._value)

    def __int__(self) -> int:
        return self._value

    def __and__(self, mask: int) -> int:
        return self._value & mask

    def __repr__(self) -> str:
        return f"SequenceNumber({self._value})"
