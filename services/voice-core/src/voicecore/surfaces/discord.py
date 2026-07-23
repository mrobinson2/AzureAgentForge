"""Discord voice surface — offline-testable pieces of phase 4.

Phase 4 of docs/notes/plans/2026-07-22-voice-track.md is Discord voice. The live
part (a gateway voice connection, Opus decode via a native lib) needs a running
bot + libopus; these are the parts that do NOT and are worth pinning by tests:

  - RTP packet parsing (RFC 3550) — pure binary structure, the wire Discord
    delivers voice on.
  - a SequenceTracker that classifies each packet (in-order / duplicate /
    reordered / gap) with 16-bit wraparound, so jitter and loss are observable
    before the decode step.

The Opus -> PCM decode itself is a native-lib dependency and is left as an
injected `opus_decode` hook (unverified, like the Voice Live provider): the
Discord adapter is structured and testable without pulling libopus into CI.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum

from ..interfaces import AudioFrame

_RTP_MIN_HEADER = 12


@dataclass(frozen=True)
class RtpPacket:
    version: int
    padding: bool
    extension: bool
    csrc_count: int
    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes


def parse_rtp_packet(data: bytes) -> RtpPacket:
    """Parse an RFC 3550 RTP packet. Raises ValueError on a runt packet or a
    non-v2 version (Discord always sends RTP v2)."""
    if len(data) < _RTP_MIN_HEADER:
        raise ValueError(f"RTP packet too short: {len(data)} < {_RTP_MIN_HEADER}")
    b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:_RTP_MIN_HEADER])
    version = b0 >> 6
    if version != 2:
        raise ValueError(f"unsupported RTP version {version} (expected 2)")
    csrc_count = b0 & 0x0F
    header_len = _RTP_MIN_HEADER + csrc_count * 4
    if len(data) < header_len:
        raise ValueError("RTP packet truncated within CSRC list")
    return RtpPacket(
        version=version,
        padding=bool(b0 & 0x20),
        extension=bool(b0 & 0x10),
        csrc_count=csrc_count,
        marker=bool(b1 & 0x80),
        payload_type=b1 & 0x7F,
        sequence=seq,
        timestamp=ts,
        ssrc=ssrc,
        payload=data[header_len:],
    )


class PacketOrder(Enum):
    IN_ORDER = "in_order"
    DUPLICATE = "duplicate"
    REORDERED = "reordered"  # older than the last seen
    GAP = "gap"              # newer, but sequence numbers were skipped


class SequenceTracker:
    """Classify RTP sequence numbers with 16-bit wraparound. `observe` returns
    the packet's relationship to the highest in-order sequence seen so far;
    `missing` counts skipped numbers across GAPs (a loss estimate)."""

    def __init__(self) -> None:
        self._last: int | None = None
        self.missing = 0

    def observe(self, sequence: int) -> PacketOrder:
        seq = sequence & 0xFFFF
        if self._last is None:
            self._last = seq
            return PacketOrder.IN_ORDER
        diff = (seq - self._last) & 0xFFFF
        if diff == 0:
            return PacketOrder.DUPLICATE
        if diff < 0x8000:
            # forward
            if diff > 1:
                self.missing += diff - 1
                order = PacketOrder.GAP
            else:
                order = PacketOrder.IN_ORDER
            self._last = seq
            return order
        # diff >= 0x8000 -> the new seq is behind the last: a late/reordered packet
        return PacketOrder.REORDERED


def frame_from_rtp(pkt: RtpPacket, opus_decode=None) -> AudioFrame:
    """Turn an RTP packet's Opus payload into an AudioFrame. Requires an
    injected `opus_decode(payload) -> (pcm: bytes, energy: float)`; without one
    it raises (the native Opus dependency is not pulled into the offline core —
    mirrors the Voice Live provider's unverified posture)."""
    if opus_decode is None:
        raise NotImplementedError(
            "Opus decode requires a native decoder (inject opus_decode); the "
            "Discord voice decode path is unverified until wired against libopus"
        )
    pcm, energy = opus_decode(pkt.payload)
    return AudioFrame(energy=energy, pcm=pcm)
