"""voice-core providers. Phase 1 ships the offline `fake` provider only; real
STT/TTS adapters (Microsoft Voice Live, others) land as separate modules behind
the same interfaces."""

from .fake import FakeSynthesizer, FakeTranscriber

__all__ = ["FakeTranscriber", "FakeSynthesizer"]
