"""Discord voice surface — RTP parse + sequence tracking, offline."""

import pathlib
import struct
import sys

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from voicecore.interfaces import AudioFrame  # noqa: E402
from voicecore.surfaces.discord import (  # noqa: E402
    PacketOrder,
    SequenceTracker,
    frame_from_rtp,
    parse_rtp_packet,
)


def _rtp(seq=1, ts=160, ssrc=0xDEAD, pt=120, cc=0, payload=b"opus", marker=False):
    b0 = (2 << 6) | (cc & 0x0F)
    b1 = (0x80 if marker else 0) | (pt & 0x7F)
    header = struct.pack("!BBHII", b0, b1, seq, ts, ssrc)
    header += b"\x00\x00\x00\x00" * cc  # csrc identifiers
    return header + payload


# ── RTP parsing ───────────────────────────────────────────────────────────────

def test_parse_basic_header_and_payload():
    pkt = parse_rtp_packet(_rtp(seq=42, ts=960, ssrc=0x1234, pt=120, payload=b"xyz"))
    assert pkt.version == 2
    assert pkt.sequence == 42
    assert pkt.timestamp == 960
    assert pkt.ssrc == 0x1234
    assert pkt.payload_type == 120
    assert pkt.payload == b"xyz"


def test_parse_with_csrc_offsets_payload():
    pkt = parse_rtp_packet(_rtp(cc=2, payload=b"data"))
    assert pkt.csrc_count == 2
    assert pkt.payload == b"data"  # 8 CSRC bytes skipped


def test_parse_marker_bit():
    assert parse_rtp_packet(_rtp(marker=True)).marker is True
    assert parse_rtp_packet(_rtp(marker=False)).marker is False


def test_parse_runt_raises():
    with pytest.raises(ValueError):
        parse_rtp_packet(b"\x80\x78\x00")


def test_parse_wrong_version_raises():
    data = bytearray(_rtp())
    data[0] = 0x40  # version 1
    with pytest.raises(ValueError):
        parse_rtp_packet(bytes(data))


def test_parse_truncated_csrc_raises():
    # claims 4 CSRCs (16 bytes) but only the 12-byte base header present
    data = struct.pack("!BBHII", (2 << 6) | 4, 120, 1, 0, 0)
    with pytest.raises(ValueError):
        parse_rtp_packet(data)


# ── sequence tracking ─────────────────────────────────────────────────────────

def test_in_order_and_duplicate_and_gap():
    t = SequenceTracker()
    assert t.observe(10) is PacketOrder.IN_ORDER  # first
    assert t.observe(11) is PacketOrder.IN_ORDER
    assert t.observe(11) is PacketOrder.DUPLICATE
    assert t.observe(15) is PacketOrder.GAP       # 12,13,14 missing
    assert t.missing == 3


def test_reordered_is_detected_without_advancing():
    t = SequenceTracker()
    t.observe(100)
    t.observe(101)
    assert t.observe(99) is PacketOrder.REORDERED
    # a late packet does not move the high-water mark
    assert t.observe(102) is PacketOrder.IN_ORDER


def test_sequence_wraparound_is_in_order():
    t = SequenceTracker()
    t.observe(0xFFFF)
    assert t.observe(0x0000) is PacketOrder.IN_ORDER  # 65535 -> 0 wraps forward
    assert t.observe(0x0001) is PacketOrder.IN_ORDER


# ── decode hook ───────────────────────────────────────────────────────────────

def test_frame_from_rtp_requires_decoder():
    pkt = parse_rtp_packet(_rtp(payload=b"opusdata"))
    with pytest.raises(NotImplementedError):
        frame_from_rtp(pkt)


def test_frame_from_rtp_with_injected_decoder():
    pkt = parse_rtp_packet(_rtp(payload=b"opusdata"))
    frame = frame_from_rtp(pkt, opus_decode=lambda p: (b"pcm", 0.42))
    assert isinstance(frame, AudioFrame)
    assert frame.energy == 0.42
    assert frame.pcm == b"pcm"
