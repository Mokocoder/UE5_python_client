# net/replication/rep_handle_map.py
"""Data-driven RepLayout property definitions from rep_layout.json"""
from __future__ import annotations

import json
from pathlib import Path

from net.replication.types import PropertyType, PropertyDef
from net.net_serialization import (
    read_quantized_vector,
    read_vector_double,
    read_vector_fixed_normal,
    read_rotation_compressed_short,
    read_rotator_smart_pitch,
    read_rep_movement,
    read_gameplay_ability_rep_anim_montage,
)
from net.identity.unique_net_id_repl import FUniqueNetIdRepl

_DATA_PATH = Path(__file__).parent / "data" / "rep_layout.json"


_TYPE_MAP: dict[str, PropertyType] = {
    "bool":      PropertyType.BOOL,
    "byte":      PropertyType.BYTE,
    "float":     PropertyType.FLOAT,
    "double":    PropertyType.DOUBLE,
    "string":    PropertyType.STRING,
    "int8":      PropertyType.INT8,
    "int16":     PropertyType.INT16,
    "int32":     PropertyType.INT,
    "int64":     PropertyType.INT64,
    "uint16":    PropertyType.UINT16,
    "uint32":    PropertyType.UINT32,
    "uint64":    PropertyType.UINT64,
    "object":    PropertyType.OBJECT,
    "soft_obj":  PropertyType.SOFT_OBJECT,
    "weak_obj":  PropertyType.WEAK_OBJECT,
    "interface": PropertyType.INTERFACE,
    "class_ref": PropertyType.CLASS,
    "name":      PropertyType.NAME,
    "enum":      PropertyType.BYTE,
}


_STRUCT_SERIALIZERS = {
    # Vectors
    "struct:Vector":                        lambda r, _: read_vector_double(r),
    "struct:Vector_NetQuantize":            lambda r, _: read_quantized_vector(r, 1),
    "struct:Vector_NetQuantize10":          lambda r, _: read_quantized_vector(r, 10),
    "struct:Vector_NetQuantize100":         lambda r, _: read_quantized_vector(r, 100),

    # Rotators
    "struct:Rotator":                       lambda r, _: read_rotation_compressed_short(r),
    "struct:Rotator_NetQuantize":           lambda r, _: read_rotation_compressed_short(r),
    "struct:Rotator_NetQuantizeSmartPitch": lambda r, _: read_rotator_smart_pitch(r),

    # Compound structs
    "struct:RepMovement":                   lambda r, _: read_rep_movement(r),
    "struct:UniqueNetIdRepl":               lambda r, _: FUniqueNetIdRepl.read(r),
    "struct:Vector_NetQuantizeNormal":      lambda r, _: read_vector_fixed_normal(r),
    "struct:GameplayAbilityRepAnimMontage": lambda r, _: read_gameplay_ability_rep_anim_montage(r),
}


_MISSING_ENUM_BITS: set[str] = set()
_MISSING_STRUCT_SERIALIZERS: set[str] = set()
_SKIPPED_TYPES: set[str] = set()


def _load_data() -> dict:
    with open(_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


_DATA = _load_data()
_HANDLE_MAPS: dict[str, list[dict]] = _DATA.get("handle_maps", {})
_HANDLE_COUNTS: dict[str, int] = _DATA.get("handle_counts", {})
_PARENT_MAP: dict[str, str] = _DATA.get("parent_map", {})


def _build_entries(entries: list[dict], overrides: dict, exclude: set) -> list[PropertyDef]:
    """Map JSON handle entries to PropertyDef list."""
    defs: list[PropertyDef] = []

    for entry in entries:
        h: int = entry["h"]

        if h in exclude:
            continue
        if h in overrides:
            defs.append(overrides[h])
            continue

        type_str: str = entry["type"]
        name: str = entry["name"]

        # 1) Dynamic array / map / set — recurse into inner entries
        if type_str in ("array", "map", "set"):
            inner_entries = entry.get("inner", [])
            inner_defs = _build_entries(inner_entries, {}, set()) if inner_entries else []
            defs.append(PropertyDef(
                name=name, prop_type=PropertyType.DYNAMIC_ARRAY,
                handle=h, inner_defs=inner_defs,
            ))
            continue

        # 2) Struct / complex types with custom serializer
        serializer = _STRUCT_SERIALIZERS.get(type_str)
        if serializer is not None:
            defs.append(PropertyDef(
                name=name, prop_type=PropertyType.STRUCT,
                handle=h, serializer=serializer,
            ))
            continue

        # 3) Struct with NetSerializer but no client-side deserializer
        if type_str.startswith("struct:"):
            struct_tag = type_str.split(":", 1)[1] if ":" in type_str else type_str
            if struct_tag not in _MISSING_STRUCT_SERIALIZERS:
                _MISSING_STRUCT_SERIALIZERS.add(struct_tag)
                print(f"[REPLAYOUT-WARN] No serializer for {type_str} (handle {h}, {name}) — property will be unreadable")
            defs.append(PropertyDef(
                name=name, prop_type=PropertyType.STRUCT,
                handle=h,
            ))
            continue

        # 4) Primitive types
        prop_type = _TYPE_MAP.get(type_str)
        if prop_type is not None:
            max_val = entry.get("max")
            if max_val is not None:
                mv = max_val
                defs.append(PropertyDef(
                    name=name, prop_type=PropertyType.BYTE,
                    handle=h, serializer=lambda r, _, m=mv: r.read_int_wrapped(m),
                ))
            elif "enum" in entry:
                # Enum property without max value — fall back to 8 bits
                _MISSING_ENUM_BITS.add(entry["enum"])
                defs.append(PropertyDef(name=name, prop_type=prop_type, handle=h))
            else:
                defs.append(PropertyDef(name=name, prop_type=prop_type, handle=h))
            continue

        # 5) Unrecognized type — warn once
        if type_str not in _SKIPPED_TYPES:
            _SKIPPED_TYPES.add(type_str)
            print(f"[REPLAYOUT-WARN] Unrecognized type {type_str!r} (handle {h}, {name}) — skipped")

    return defs


def get_total_handles(class_name: str) -> int:
    """Return total handle count for *class_name* from handle_counts."""
    return _HANDLE_COUNTS.get(class_name, 0)


def build_property_defs(
    class_name: str,
    *,
    overrides: dict[int, PropertyDef] | None = None,
    exclude: set[int] | None = None,
) -> list[PropertyDef]:
    """Build PropertyDef list from the merged rep_layout.json"""
    entries = _HANDLE_MAPS.get(class_name)
    if entries is None:
        raise KeyError(f"Class {class_name!r} not found in rep_layout.json")

    overrides = overrides or {}
    exclude = exclude or set()

    # If BP class, prepend parent defs (no overrides/exclude for parent)
    parent = _PARENT_MAP.get(class_name)
    parent_defs = build_property_defs(parent) if parent else []

    own_defs = _build_entries(entries, overrides, exclude)
    return parent_defs + own_defs
