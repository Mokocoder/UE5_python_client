# net/replication/struct_serializers/gas.py
"""GAS (Gameplay Ability System) struct serializers."""
from __future__ import annotations

from typing import TYPE_CHECKING

from constants import (
    ENGINE_NET_VER_CURRENT,
    ENGINE_NET_VER_DYNAMIC_MONTAGE_SERIALIZATION,
    ENGINE_NET_VER_MONTAGE_PLAY_INST_ID,
    ENGINE_NET_VER_MONTAGE_PLAY_COUNT_SERIALIZATION,
)
from net.net_serialization import read_network_guid, read_prediction_key, read_fname

if TYPE_CHECKING:
    from serialization.bit_reader import FBitReader


def read_gameplay_ability_rep_anim_montage(reader: 'FBitReader', engine_ver: int = ENGINE_NET_VER_CURRENT) -> dict:
    """FGameplayAbilityRepAnimMontage::NetSerialize — version-aware."""
    result: dict = {}

    b_is_montage = True
    if engine_ver >= ENGINE_NET_VER_DYNAMIC_MONTAGE_SERIALIZATION:
        b_is_montage = bool(reader.read_bit())

    b_rep_position = reader.read_bit()
    if b_rep_position:
        result['Position'] = reader.read_float()
    else:
        result['SectionIdToPlay'] = reader.serialize_bits(7)[0] & 0x7F

    result['IsStopped'] = bool(reader.read_bit())

    if engine_ver < ENGINE_NET_VER_MONTAGE_PLAY_INST_ID:
        reader.read_bit()  # bForcePlayBit (deprecated)

    result['SkipPositionCorrection'] = bool(reader.read_bit())
    result['bSkipPlayRate'] = bool(reader.read_bit())

    result['Animation'] = read_network_guid(reader)
    result['PlayRate'] = reader.read_float()
    result['BlendTime'] = reader.read_float()
    result['NextSectionID'] = reader.read_byte()

    if engine_ver >= ENGINE_NET_VER_MONTAGE_PLAY_INST_ID:
        result['PlayInstanceId'] = reader.read_byte()

    result['PredictionKey'] = read_prediction_key(reader, engine_ver)

    if not b_is_montage:
        result['BlendOutTime'] = reader.read_float()
        result['SlotName'] = read_fname(reader)

    if engine_ver >= ENGINE_NET_VER_MONTAGE_PLAY_COUNT_SERIALIZATION:
        result['PlayCount'] = reader.read_float()

    return result


STRUCT_SERIALIZERS: list[tuple[str, object]] = [
    ("struct:GameplayAbilityRepAnimMontage", lambda r, _: read_gameplay_ability_rep_anim_montage(r)),
]
