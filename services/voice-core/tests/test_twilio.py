"""Twilio surface — PIN gate + Media Streams codec, offline."""

import base64
import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from voicecore.interfaces import AudioFrame  # noqa: E402
from voicecore.surfaces.twilio import (  # noqa: E402
    PinGate,
    PinState,
    TwilioEventKind,
    frame_energy_from_ulaw,
    outbound_media,
    parse_twilio_message,
    ulaw_to_linear,
)


# ── μ-law + energy ────────────────────────────────────────────────────────────

def test_ulaw_silence_vs_loud_energy():
    # 0xFF is the μ-law code nearest zero; 0x00 is near full-scale.
    quiet = bytes([0xFF] * 8)
    loud = bytes([0x00] * 8)
    assert frame_energy_from_ulaw(quiet) < frame_energy_from_ulaw(loud)
    assert frame_energy_from_ulaw(b"") == 0.0
    assert 0.0 <= frame_energy_from_ulaw(loud) <= 1.0


def test_ulaw_decode_sign():
    # 0x80 and 0x00 are the two full-scale codes, opposite polarity, |peak|.
    assert ulaw_to_linear(0x80) > 0
    assert ulaw_to_linear(0x00) < 0
    assert abs(ulaw_to_linear(0x80)) == abs(ulaw_to_linear(0x00))


# ── inbound event parsing ─────────────────────────────────────────────────────

def test_parse_start():
    e = parse_twilio_message({"event": "start", "start": {"streamSid": "MZ1"}})
    assert e.kind is TwilioEventKind.START
    assert e.stream_sid == "MZ1"


def test_parse_media_yields_frame():
    payload = base64.b64encode(bytes([0x00] * 8)).decode()
    e = parse_twilio_message({"event": "media", "media": {"payload": payload}})
    assert e.kind is TwilioEventKind.MEDIA
    assert isinstance(e.frame, AudioFrame)
    assert e.frame.energy > 0.0
    assert e.frame.pcm == bytes([0x00] * 8)


def test_parse_media_malformed_payload_is_silence():
    e = parse_twilio_message({"event": "media", "media": {"payload": "!!!not-base64"}})
    assert e.kind is TwilioEventKind.MEDIA
    assert e.frame.energy == 0.0


def test_parse_dtmf_and_stop_and_unknown():
    assert parse_twilio_message({"event": "dtmf", "dtmf": {"digit": "5"}}).digit == "5"
    assert parse_twilio_message({"event": "stop"}).kind is TwilioEventKind.STOP
    assert parse_twilio_message({"event": "mark"}).kind is TwilioEventKind.UNKNOWN
    assert parse_twilio_message({}).kind is TwilioEventKind.UNKNOWN


def test_outbound_media_round_trips_payload():
    frame = AudioFrame(energy=0.0, pcm=b"\x01\x02\x03")
    msg = outbound_media(frame, "MZ9")
    assert msg["event"] == "media"
    assert msg["streamSid"] == "MZ9"
    assert base64.b64decode(msg["media"]["payload"]) == b"\x01\x02\x03"


# ── PIN gate ──────────────────────────────────────────────────────────────────

def test_correct_pin_unlocks():
    g = PinGate("1234")
    for d in "1234":
        g.press(d)
    assert g.unlocked is True
    assert g.state is PinState.UNLOCKED


def test_starts_locked_and_engages_only_after_pin():
    g = PinGate("99")
    assert g.unlocked is False  # fail-closed: agent not engaged yet
    g.press("9")
    assert g.unlocked is False
    g.press("9")
    assert g.unlocked is True


def test_wrong_pin_attempts_then_lockout():
    g = PinGate("11", max_attempts=2)
    g.press("0"); g.press("0")  # attempt 1 wrong
    assert g.state is PinState.COLLECTING
    g.press("2"); g.press("2")  # attempt 2 wrong -> lockout
    assert g.locked_out is True
    # locked out never unlocks, even with the right pin
    g.press("1"); g.press("1")
    assert g.unlocked is False


def test_press_after_unlock_is_noop():
    g = PinGate("12")
    g.press("1"); g.press("2")
    assert g.unlocked
    assert g.press("9") is PinState.UNLOCKED
