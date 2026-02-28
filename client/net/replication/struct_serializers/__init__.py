# net/replication/struct_serializers/__init__.py
"""Collect all struct serializer extensions."""
from net.replication.struct_serializers.gas import STRUCT_SERIALIZERS as _gas

ALL_STRUCT_SERIALIZERS: dict[str, object] = dict(_gas)
