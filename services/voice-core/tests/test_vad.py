"""VAD state-machine tests — pure, offline."""

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from voicecore.interfaces import AudioFrame, VadEvent  # noqa: E402
from voicecore.vad import Vad, VadConfig  # noqa: E402


def _loud():
    return AudioFrame(energy=0.8)


def _quiet():
    return AudioFrame(energy=0.01)


def test_speech_start_on_first_loud_frame():
    vad = Vad(VadConfig(silence_hangover_frames=2))
    assert vad.process(_quiet()) is None
    assert vad.process(_loud()) is VadEvent.SPEECH_START
    assert vad.in_speech is True


def test_speech_end_requires_full_hangover():
    vad = Vad(VadConfig(silence_hangover_frames=3))
    vad.process(_loud())  # SPEECH_START
    # A single quiet frame mid-utterance must NOT end the turn.
    assert vad.process(_quiet()) is None
    assert vad.process(_loud()) is None  # resumes, resets the silence run
    assert vad.process(_quiet()) is None
    assert vad.process(_quiet()) is None
    assert vad.process(_quiet()) is VadEvent.SPEECH_END
    assert vad.in_speech is False


def test_barge_in_when_tts_active():
    vad = Vad()
    # Speech starting while TTS is playing is a BARGE_IN, not a plain start.
    assert vad.process(_loud(), tts_active=True) is VadEvent.BARGE_IN


def test_plain_start_when_tts_idle():
    vad = Vad()
    assert vad.process(_loud(), tts_active=False) is VadEvent.SPEECH_START


def test_threshold_boundary_is_inclusive():
    vad = Vad(VadConfig(speech_energy_threshold=0.15))
    assert vad.process(AudioFrame(energy=0.15)) is VadEvent.SPEECH_START


def test_reset_returns_to_silence():
    vad = Vad()
    vad.process(_loud())
    vad.reset()
    assert vad.in_speech is False
