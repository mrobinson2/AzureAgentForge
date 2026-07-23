"""voice-core — provider-agnostic contracts + value types.

Phase 1 of docs/notes/plans/2026-07-22-voice-track.md. The pipeline
(pipeline.py) is written against these Protocols only; concrete STT/TTS/VAD
providers (Microsoft Voice Live, others) are pluggable adapters that never
leak into the core. Mirrors the model-router's provider-agnostic posture.

Everything here is pure data + typing — no I/O, no cloud SDK — so the whole
turn loop is unit-testable offline against the fake provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AudioFrame:
    """One fixed-duration chunk of PCM audio. `energy` is a normalized 0..1
    loudness estimate the VAD reads; real adapters compute it (e.g. RMS), the
    fake provider sets it directly. `pcm` is opaque to the core."""

    energy: float
    pcm: bytes = b""
    is_final: bool = False


@dataclass(frozen=True)
class Transcript:
    """A recognized utterance. `is_final` distinguishes a stable result from an
    interim hypothesis (streaming STT emits many interims per final)."""

    text: str
    is_final: bool = True
    confidence: float = 1.0


@dataclass(frozen=True)
class TurnResult:
    """The outcome of one user turn through the pipeline."""

    transcript: str
    reply_text: str
    output_frames: list[AudioFrame] = field(default_factory=list)
    barged_in: bool = False


class VadEvent(Enum):
    """Voice-activity transitions the pipeline reacts to."""

    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"
    BARGE_IN = "barge_in"  # user started speaking while TTS was playing


@runtime_checkable
class Transcriber(Protocol):
    """Streaming or batch STT. `transcribe` turns buffered speech frames into a
    final Transcript."""

    def transcribe(self, frames: list[AudioFrame]) -> Transcript: ...


@runtime_checkable
class Synthesizer(Protocol):
    """TTS. `synthesize` turns reply text into a list of audio frames to play."""

    def synthesize(self, text: str) -> list[AudioFrame]: ...


@runtime_checkable
class PersonaOverlay(Protocol):
    """Applies an agent persona to a user utterance before it reaches the agent
    turn (voice_id/vibe live in AGENTS.md frontmatter; this is the text side)."""

    def apply(self, utterance: str) -> str: ...
