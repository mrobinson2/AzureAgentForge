"""Voice-activity detection — a pure state machine.

No audio libraries, no I/O: it consumes AudioFrame.energy and emits VadEvents.
This is the piece that makes barge-in correct — a SPEECH_START while TTS is
playing is reported as BARGE_IN so the pipeline can cancel in-flight synthesis.

Debounced end-of-speech: a single sub-threshold frame does not end a turn;
`silence_hangover_frames` consecutive silent frames do. Prevents a brief pause
mid-sentence from cutting the user off.
"""

from __future__ import annotations

from dataclasses import dataclass

from .interfaces import AudioFrame, VadEvent


@dataclass(frozen=True)
class VadConfig:
    # Frame energy (0..1) at or above which a frame counts as speech.
    speech_energy_threshold: float = 0.15
    # Consecutive silent frames required to declare SPEECH_END (debounce).
    silence_hangover_frames: int = 3


class Vad:
    """Two-state (SILENCE / SPEECH) activity detector.

    `process(frame, tts_active)` returns exactly one VadEvent or None per frame:
      - SILENCE + speech            -> SPEECH_START (or BARGE_IN if tts_active)
      - SPEECH  + hangover of silence -> SPEECH_END
    """

    _SILENCE = "silence"
    _SPEECH = "speech"

    def __init__(self, config: VadConfig | None = None) -> None:
        self.config = config or VadConfig()
        self._state = self._SILENCE
        self._silence_run = 0

    @property
    def in_speech(self) -> bool:
        return self._state == self._SPEECH

    def reset(self) -> None:
        self._state = self._SILENCE
        self._silence_run = 0

    def process(self, frame: AudioFrame, tts_active: bool = False) -> VadEvent | None:
        is_speech = frame.energy >= self.config.speech_energy_threshold

        if self._state == self._SILENCE:
            if is_speech:
                self._state = self._SPEECH
                self._silence_run = 0
                return VadEvent.BARGE_IN if tts_active else VadEvent.SPEECH_START
            return None

        # _SPEECH
        if is_speech:
            self._silence_run = 0
            return None
        self._silence_run += 1
        if self._silence_run >= self.config.silence_hangover_frames:
            self._state = self._SILENCE
            self._silence_run = 0
            return VadEvent.SPEECH_END
        return None
