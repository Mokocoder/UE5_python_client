# net/packets/out_bunch.py
"""FOutBunch - Outgoing bunch."""
from __future__ import annotations
from serialization.bit_writer import FBitWriter
from net.channels.channel_types import EChannelCloseReason
from constants import MAX_BUNCH_DATA_BITS


class FOutBunch(FBitWriter):
    def __init__(self, max_bits: int = MAX_BUNCH_DATA_BITS, allow_resize: bool = True):
        super().__init__(max_bits, allow_resize=allow_resize)

        self.ChIndex: int = 0
        self.ChSequence: int = 0
        self.ChNameIndex: int = 0

        self.bOpen: bool = False
        self.bClose: bool = False
        self.bIsReplicationPaused: bool = False
        self.bReliable: bool = False
        self.bPartial: bool = False
        self.bPartialInitial: bool = False
        self.bPartialFinal: bool = False
        self.bPartialCustomExportsFinal: bool = False
        self.bHasPackageMapExports: bool = False
        self.bHasMustBeMappedGUIDs: bool = False
        self.bDormant: bool = False

        self.CloseReason: EChannelCloseReason = EChannelCloseReason.Destroyed

        self.PacketId: int = -1
        self.ReceivedAck: bool = False

        self.ExportNetGUIDs: list = []
        self.NetFieldExports: list = []
