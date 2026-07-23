"""Turn-loop tests — full pipeline over the fake provider, offline."""

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from voicecore.interfaces import AudioFrame, VadEvent  # noqa: E402
from voicecore.pipeline import VoicePipeline  # noqa: E402
from voicecore.providers.fake import FakeSynthesizer, FakeTranscriber  # noqa: E402
from voicecore.vad import Vad, VadConfig  # noqa: E402


def _loud():
    return AudioFrame(energy=0.8)


def _quiet():
    return AudioFrame(energy=0.0)


def _pipeline(agent_fn, canned="hello there"):
    return VoicePipeline(
        transcriber=FakeTranscriber(canned=canned),
        synthesizer=FakeSynthesizer(),
        agent_fn=agent_fn,
        vad=Vad(VadConfig(silence_hangover_frames=2)),
    )


def test_full_turn_produces_transcript_reply_and_playback():
    seen = []

    def agent(prompt):
        seen.append(prompt)
        return "hi back"

    p = _pipeline(agent, canned="good morning")
    # speak (2 loud), then go quiet for the hangover to end the turn.
    p.feed(_loud())
    p.feed(_loud())
    p.feed(_quiet())
    end = p.feed(_quiet())  # SPEECH_END -> turn runs here

    assert end.event is VadEvent.SPEECH_END
    assert end.turn is not None
    assert end.turn.transcript == "good morning"
    assert end.turn.reply_text == "hi back"
    assert len(end.turn.output_frames) == 2  # "hi back" -> 2 words
    # persona overlay reached the agent with the spoken utterance embedded.
    assert "good morning" in seen[0]


def test_tts_frames_drain_on_idle_ticks():
    p = _pipeline(lambda _: "one two three")
    p.feed(_loud())
    p.feed(_quiet())
    p.feed(_quiet())  # SPEECH_END queues 3 output frames
    played = []
    for _ in range(5):  # idle ticks drain the queue, one frame per tick
        tick = p.feed(_quiet())
        if tick.output_frame is not None:
            played.append(tick.output_frame.pcm.decode())
    assert played == ["one", "two", "three"]
    assert p.tts_playing is False


def test_barge_in_cancels_in_flight_synthesis():
    p = _pipeline(lambda _: "a fairly long spoken reply here")
    p.feed(_loud())
    p.feed(_quiet())
    p.feed(_quiet())  # SPEECH_END -> 6 output frames queued
    assert p.tts_playing is True
    p.feed(_quiet())  # play one frame
    # user interrupts while TTS still queued
    tick = p.feed(_loud())
    assert tick.event is VadEvent.BARGE_IN
    assert p.tts_playing is False  # remaining synthesis cancelled


def test_two_sequential_turns():
    replies = iter(["first reply", "second reply"])
    p = _pipeline(lambda _: next(replies), canned="x")
    # turn 1
    p.feed(_loud())
    p.feed(_quiet())
    t1 = p.feed(_quiet())
    # drain
    for _ in range(3):
        p.feed(_quiet())
    # turn 2
    p.feed(_loud())
    p.feed(_quiet())
    t2 = p.feed(_quiet())
    assert t1.turn.reply_text == "first reply"
    assert t2.turn.reply_text == "second reply"
