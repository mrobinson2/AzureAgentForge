"""Transport-agnostic voice session — phase 2 (web surface core).

The web-widget / Twilio / Discord surfaces differ only in how audio frames move
on and off the wire. This layer is the part that does NOT differ: it pumps a
`Transport` (any duplex frame stream) through a `VoicePipeline`, gated by
explicit consent and tracking a recording flag. Pure and synchronous, so it is
offline-testable against an in-memory fake transport — the real WebSocket /
WebRTC bridge only has to implement `recv`/`send`.

Consent is a hard gate from day one (the web plan requires a consent banner +
recording indicator): no user frame is processed and nothing is recorded until
`grant_consent()` is called. Revoking consent mid-session stops processing and
recording immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from .interfaces import AudioFrame
from .pipeline import PipelineTick, VoicePipeline


@runtime_checkable
class Transport(Protocol):
    """A duplex audio frame stream. `recv` returns the next inbound frame or
    None when none is currently available (or the stream closed); `send` plays
    an outbound frame."""

    def recv(self) -> AudioFrame | None: ...
    def send(self, frame: AudioFrame) -> None: ...


class SessionState(Enum):
    AWAITING_CONSENT = "awaiting_consent"
    ACTIVE = "active"
    CLOSED = "closed"


@dataclass
class PumpResult:
    """Outcome of pumping one inbound frame."""

    state: SessionState
    had_frame: bool = False  # transport.recv returned a frame this pump
    processed: bool = False  # frame reached the pipeline
    tick: PipelineTick | None = None


@dataclass
class WebVoiceSession:
    pipeline: VoicePipeline
    transport: Transport
    record: bool = False
    _state: SessionState = field(default=SessionState.AWAITING_CONSENT, init=False)
    _recorded: list[AudioFrame] = field(default_factory=list, init=False)

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def recording(self) -> bool:
        # Recording is only ever true while active AND the flag is set — an
        # unconsented or closed session records nothing.
        return self.record and self._state is SessionState.ACTIVE

    @property
    def recorded_frames(self) -> list[AudioFrame]:
        return list(self._recorded)

    def grant_consent(self) -> None:
        if self._state is SessionState.AWAITING_CONSENT:
            self._state = SessionState.ACTIVE

    def revoke_consent(self) -> None:
        # Stop processing + recording immediately; a revoked session is closed.
        self._state = SessionState.CLOSED

    def purge_recording(self) -> int:
        """Delete retained frames (the retention-policy hook). Returns how many
        were dropped."""
        n = len(self._recorded)
        self._recorded.clear()
        return n

    def pump_once(self) -> PumpResult:
        frame = self.transport.recv()
        if frame is None:
            return PumpResult(state=self._state, had_frame=False)
        if self._state is not SessionState.ACTIVE:
            # Pre-consent or closed: drop the frame, record nothing, process
            # nothing. The caller keeps showing the consent banner.
            return PumpResult(state=self._state, had_frame=True, processed=False)
        if self.record:
            self._recorded.append(frame)
        tick = self.pipeline.feed(frame)
        if tick.output_frame is not None:
            self.transport.send(tick.output_frame)
        return PumpResult(state=self._state, had_frame=True, processed=True, tick=tick)

    def run(self, max_ticks: int = 10_000) -> list[PumpResult]:
        """Pump until the transport yields no frame (drained) or max_ticks."""
        results: list[PumpResult] = []
        for _ in range(max_ticks):
            r = self.pump_once()
            if not r.had_frame:
                break
            results.append(r)
        return results
