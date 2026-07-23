"""Twilio phone surface — offline-testable pieces of phase 3.

Phase 3 of docs/notes/plans/2026-07-22-voice-track.md is the Twilio phone line.
The live parts (a real number, a running Media Streams websocket) need a Twilio
account; these are the parts that do NOT — and that carry the compliance
weight, so they are worth having pinned by tests before the wire is live:

  - PinGate: the PIN a caller must enter (DTMF) before the agent engages.
  - the Media Streams codec: parse inbound Twilio websocket events into typed
    events + AudioFrames (stdlib G.711 μ-law decode for the VAD's energy), and
    build outbound `media` messages.

No Twilio SDK, no network — pure functions + a small state machine.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum

from ..interfaces import AudioFrame

# G.711 μ-law: peak decoded magnitude, used to normalize energy into 0..1.
_ULAW_PEAK = 32124


def ulaw_to_linear(ulaw_byte: int) -> int:
    """Decode one G.711 μ-law byte to a signed 16-bit linear PCM sample."""
    u = ~ulaw_byte & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = ((mantissa << 3) + 0x84) << exponent
    sample -= 0x84
    return -sample if sign else sample


def frame_energy_from_ulaw(payload: bytes) -> float:
    """Normalized 0..1 loudness of a μ-law payload — the value the VAD reads."""
    if not payload:
        return 0.0
    total = sum(abs(ulaw_to_linear(b)) for b in payload)
    return min(1.0, (total / len(payload)) / _ULAW_PEAK)


class TwilioEventKind(Enum):
    START = "start"
    MEDIA = "media"
    DTMF = "dtmf"
    STOP = "stop"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TwilioEvent:
    kind: TwilioEventKind
    frame: AudioFrame | None = None
    digit: str | None = None
    stream_sid: str | None = None


def parse_twilio_message(msg: dict) -> TwilioEvent:
    """Turn one Twilio Media Streams websocket message into a typed event.

    Media payloads (base64 μ-law) become an AudioFrame carrying the raw μ-law
    bytes as pcm and a decoded energy estimate. Unknown events degrade to
    UNKNOWN rather than raising — a resilient wire parser."""
    event = (msg or {}).get("event")
    if event == "start":
        return TwilioEvent(
            kind=TwilioEventKind.START,
            stream_sid=(msg.get("start") or {}).get("streamSid") or msg.get("streamSid"),
        )
    if event == "media":
        payload_b64 = (msg.get("media") or {}).get("payload", "")
        try:
            raw = base64.b64decode(payload_b64) if payload_b64 else b""
        except Exception:  # noqa: BLE001 — malformed frame → silence, never crash
            raw = b""
        return TwilioEvent(
            kind=TwilioEventKind.MEDIA,
            frame=AudioFrame(energy=frame_energy_from_ulaw(raw), pcm=raw),
            stream_sid=msg.get("streamSid"),
        )
    if event == "dtmf":
        return TwilioEvent(
            kind=TwilioEventKind.DTMF,
            digit=(msg.get("dtmf") or {}).get("digit"),
            stream_sid=msg.get("streamSid"),
        )
    if event == "stop":
        return TwilioEvent(kind=TwilioEventKind.STOP, stream_sid=msg.get("streamSid"))
    return TwilioEvent(kind=TwilioEventKind.UNKNOWN)


def outbound_media(frame: AudioFrame, stream_sid: str) -> dict:
    """Build the Twilio `media` websocket message that plays `frame` — its pcm
    (μ-law bytes) base64-encoded, addressed to the stream."""
    payload_b64 = base64.b64encode(frame.pcm).decode("ascii")
    return {
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": payload_b64},
    }


class PinState(Enum):
    COLLECTING = "collecting"
    UNLOCKED = "unlocked"
    LOCKED_OUT = "locked_out"


class PinGate:
    """DTMF PIN a caller must enter before the agent engages.

    Fail-closed: starts COLLECTING (agent NOT engaged); only an exact PIN match
    unlocks. After `max_attempts` wrong PINs it LOCKS OUT and never unlocks.
    A wrong digit-run is discarded per attempt so the caller re-enters cleanly.
    """

    def __init__(self, expected_pin: str, max_attempts: int = 3) -> None:
        if not expected_pin:
            raise ValueError("expected_pin is required")
        self._expected = expected_pin
        self._max_attempts = max_attempts
        self._buf = ""
        self._attempts = 0
        self._state = PinState.COLLECTING

    @property
    def state(self) -> PinState:
        return self._state

    @property
    def unlocked(self) -> bool:
        return self._state is PinState.UNLOCKED

    @property
    def locked_out(self) -> bool:
        return self._state is PinState.LOCKED_OUT

    def press(self, digit: str) -> PinState:
        if self._state is not PinState.COLLECTING:
            return self._state
        self._buf += digit
        if len(self._buf) >= len(self._expected):
            if self._buf == self._expected:
                self._state = PinState.UNLOCKED
            else:
                self._attempts += 1
                self._buf = ""
                if self._attempts >= self._max_attempts:
                    self._state = PinState.LOCKED_OUT
        return self._state
