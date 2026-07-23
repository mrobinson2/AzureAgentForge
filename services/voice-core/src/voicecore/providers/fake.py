"""Fake STT/TTS providers — the offline test substrate.

No audio device, no cloud key. FakeTranscriber returns a canned transcript (or
one derived from the frame count); FakeSynthesizer turns text into a
deterministic list of silent frames (one per word by default). This is what
lets the whole turn loop — including barge-in — run in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..interfaces import AudioFrame, Transcript


@dataclass
class FakeTranscriber:
    """Returns `canned` when set; otherwise a placeholder naming the frame
    count, so a test can assert the buffered speech reached the transcriber."""

    canned: str | None = None

    def transcribe(self, frames: list[AudioFrame]) -> Transcript:
        text = self.canned if self.canned is not None else f"<{len(frames)} frames>"
        return Transcript(text=text, is_final=True)


@dataclass
class FakeSynthesizer:
    """One silent frame per word (bounded), each carrying its word as pcm so a
    test can inspect what was 'spoken'. `energy` is 0 (silence) so synthesized
    playback never re-triggers the VAD as user speech."""

    frames_seen: list[str] = field(default_factory=list)

    def synthesize(self, text: str) -> list[AudioFrame]:
        words = text.split() or [""]
        out = [AudioFrame(energy=0.0, pcm=w.encode("utf-8")) for w in words]
        self.frames_seen.append(text)
        return out
