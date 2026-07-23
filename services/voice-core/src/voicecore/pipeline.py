"""The turn loop — provider-agnostic, synchronous, offline-testable.

audio in -> VAD -> (on end of speech) STT -> persona -> agent turn -> TTS ->
audio out, with barge-in cancelling in-flight synthesis.

Synchronous by design: `feed(frame)` advances the machine one frame and returns
a PipelineTick (what to play this tick, any VAD event, and a completed turn when
one just finished). A real transport (web socket, Twilio media stream, Discord
RTP) drives `feed` at frame cadence; tests drive it from a list. The agent turn
itself is an injected callable, so the core never imports an LLM client.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable

from .interfaces import (
    AudioFrame,
    PersonaOverlay,
    Synthesizer,
    Transcriber,
    TurnResult,
    VadEvent,
)
from .persona import SimplePersona
from .vad import Vad

AgentFn = Callable[[str], str]


@dataclass
class PipelineTick:
    """The result of advancing the pipeline by one input frame."""

    event: VadEvent | None = None
    # A TTS frame to play this tick, or None (user speaking / nothing queued).
    output_frame: AudioFrame | None = None
    # Set on the tick a user turn completes (SPEECH_END drove a full turn).
    turn: TurnResult | None = None


class VoicePipeline:
    def __init__(
        self,
        transcriber: Transcriber,
        synthesizer: Synthesizer,
        agent_fn: AgentFn,
        persona: PersonaOverlay | None = None,
        vad: Vad | None = None,
    ) -> None:
        self._stt = transcriber
        self._tts = synthesizer
        self._agent = agent_fn
        self._persona = persona or SimplePersona()
        self._vad = vad or Vad()
        self._speech_buf: list[AudioFrame] = []
        self._out: deque[AudioFrame] = deque()

    @property
    def tts_playing(self) -> bool:
        return len(self._out) > 0

    def feed(self, frame: AudioFrame) -> PipelineTick:
        # tts_active is evaluated BEFORE this frame so a user frame that arrives
        # while synthesis is queued is classified as a barge-in.
        tts_active = self.tts_playing
        event = self._vad.process(frame, tts_active)
        tick = PipelineTick(event=event)

        if event is VadEvent.BARGE_IN:
            # Cancel in-flight synthesis; begin capturing the interrupting speech.
            self._out.clear()
            self._speech_buf = [frame]
            return tick
        if event is VadEvent.SPEECH_START:
            self._speech_buf = [frame]
            return tick
        if event is VadEvent.SPEECH_END:
            tick.turn = self._run_turn()
            return tick

        # No transition. Either mid-speech (buffer) or idle (drain one TTS frame).
        if self._vad.in_speech:
            self._speech_buf.append(frame)
        elif self._out:
            tick.output_frame = self._out.popleft()
        return tick

    def _run_turn(self) -> TurnResult:
        frames, self._speech_buf = self._speech_buf, []
        transcript = self._stt.transcribe(frames)
        prompt = self._persona.apply(transcript.text)
        reply = self._agent(prompt)
        out_frames = self._tts.synthesize(reply)
        self._out.extend(out_frames)
        return TurnResult(
            transcript=transcript.text,
            reply_text=reply,
            output_frames=list(out_frames),
        )
