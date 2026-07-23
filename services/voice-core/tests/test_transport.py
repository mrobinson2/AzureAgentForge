"""Web-transport session tests — consent gate + pump loop, offline."""

import pathlib
import sys
from collections import deque

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from voicecore.interfaces import AudioFrame  # noqa: E402
from voicecore.pipeline import VoicePipeline  # noqa: E402
from voicecore.providers.fake import FakeSynthesizer, FakeTranscriber  # noqa: E402
from voicecore.transport import SessionState, WebVoiceSession  # noqa: E402
from voicecore.vad import Vad, VadConfig  # noqa: E402


class FakeTransport:
    """In-memory duplex: `inbound` is drained by recv, `sent` captures send."""

    def __init__(self, inbound):
        self._inbound = deque(inbound)
        self.sent = []

    def recv(self):
        return self._inbound.popleft() if self._inbound else None

    def send(self, frame):
        self.sent.append(frame)


def _loud():
    return AudioFrame(energy=0.8)


def _quiet():
    return AudioFrame(energy=0.0)


def _session(transport, record=False):
    pipe = VoicePipeline(
        transcriber=FakeTranscriber(canned="hi"),
        synthesizer=FakeSynthesizer(),
        agent_fn=lambda _: "ok done",
        vad=Vad(VadConfig(silence_hangover_frames=2)),
    )
    return WebVoiceSession(pipeline=pipe, transport=transport, record=record)


def test_starts_awaiting_consent_and_drops_frames():
    t = FakeTransport([_loud(), _quiet(), _quiet()])
    s = _session(t)
    assert s.state is SessionState.AWAITING_CONSENT
    results = s.run()
    # every frame dropped pre-consent; nothing processed, nothing sent
    assert all(r.processed is False for r in results)
    assert t.sent == []
    assert s.recording is False


def test_consent_activates_and_processes_a_turn():
    # loud, quiet, quiet -> SPEECH_END; then quiets drain TTS ("ok done" -> 2 frames)
    t = FakeTransport([_loud(), _quiet(), _quiet(), _quiet(), _quiet()])
    s = _session(t)
    s.grant_consent()
    assert s.state is SessionState.ACTIVE
    s.run()
    # a turn ran and TTS frames were sent back over the transport
    assert [f.pcm.decode() for f in t.sent] == ["ok", "done"]


def test_recording_only_while_active_and_purge_clears():
    t = FakeTransport([_loud(), _quiet(), _quiet(), _quiet()])
    s = _session(t, record=True)
    # pre-consent: no recording
    assert s.recording is False
    s.grant_consent()
    assert s.recording is True
    s.run()
    assert len(s.recorded_frames) == 4  # all active frames retained
    dropped = s.purge_recording()
    assert dropped == 4
    assert s.recorded_frames == []


def test_revoke_consent_closes_and_stops_processing():
    t = FakeTransport([_loud(), _loud()])
    s = _session(t, record=True)
    s.grant_consent()
    s.revoke_consent()
    assert s.state is SessionState.CLOSED
    results = s.run()
    assert all(r.processed is False for r in results)
    assert t.sent == []
    assert s.recording is False
