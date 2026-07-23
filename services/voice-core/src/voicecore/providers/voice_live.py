"""Microsoft Voice Live STT/TTS adapter — UNVERIFIED, isolated.

Wired behind the same Transcriber / Synthesizer Protocols the pipeline uses, so
switching from `fake` to this is a one-line provider swap. Deliberately NOT
enabled anywhere: the actual streaming calls raise until a live spike confirms
the request/response contract against a real Voice Live endpoint (mirrors the
`aca-job` sandbox provider's "reconciled but unverified" posture). This keeps a
real provider present and type-checked without pretending it works.

Enable path (a later phase): implement the two `# SPIKE:` sections against the
Voice Live streaming API, drop the NotImplementedError guards, add an
integration test behind a credentials-gated marker.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..interfaces import AudioFrame, Transcript


@dataclass
class VoiceLiveConfig:
    endpoint: str = ""
    api_key: str = ""
    voice: str = "en-US-AvaNeural"
    verified: bool = False  # flip only after the live spike passes


class VoiceLiveTranscriber:
    def __init__(self, config: VoiceLiveConfig) -> None:
        self.config = config

    def transcribe(self, frames: list[AudioFrame]) -> Transcript:
        if not self.config.verified:
            raise NotImplementedError(
                "VoiceLiveTranscriber is unverified — enable only after a live "
                "spike confirms the Voice Live streaming-STT contract"
            )
        # SPIKE: stream frames to the Voice Live STT endpoint, return the final.
        raise NotImplementedError("Voice Live STT streaming not yet implemented")


class VoiceLiveSynthesizer:
    def __init__(self, config: VoiceLiveConfig) -> None:
        self.config = config

    def synthesize(self, text: str) -> list[AudioFrame]:
        if not self.config.verified:
            raise NotImplementedError(
                "VoiceLiveSynthesizer is unverified — enable only after a live "
                "spike confirms the Voice Live TTS contract"
            )
        # SPIKE: call Voice Live TTS, chunk the returned audio into AudioFrames.
        raise NotImplementedError("Voice Live TTS not yet implemented")
