# voice-core

Provider-agnostic voice turn pipeline. **Phase 1** of the voice track
(`docs/notes/plans/2026-07-22-voice-track.md`).

```
audio in ─▶ [VAD] ─▶ [STT] ─▶ [persona + agent turn] ─▶ [TTS] ─▶ audio out
                └────────── barge-in cancels in-flight TTS ─────────┘
```

The core (`src/voicecore/`) is pure Python + stdlib — no audio device, no cloud
SDK — so the whole turn loop, including barge-in, runs in CI against the `fake`
provider. Real STT/TTS providers (Microsoft Voice Live, others) are pluggable
adapters behind the `Transcriber` / `Synthesizer` Protocols and never leak into
the core.

## Pieces

| Module | Role |
|---|---|
| `interfaces.py` | Value types (`AudioFrame`, `Transcript`, `TurnResult`) + Protocols (`Transcriber`, `Synthesizer`, `PersonaOverlay`) + `VadEvent`. |
| `vad.py` | Pure two-state (silence/speech) activity detector with debounced end-of-speech and barge-in reporting. |
| `pipeline.py` | Synchronous turn loop. `feed(frame) -> PipelineTick`. The agent turn is an injected callable — the core never imports an LLM client. |
| `persona.py` | Minimal text-side persona overlay. |
| `providers/fake.py` | Offline STT/TTS substrate for tests. |
| `providers/voice_live.py` | Microsoft Voice Live adapter — **UNVERIFIED**, isolated, disabled until a live spike. |

## Test

```bash
python -m pytest services/voice-core/tests -q   # 10 tests, fully offline
```

## Not in phase 1 (see the plan)

Transports (web widget, Twilio, Discord), a verified live provider, per-turn
latency observability, consent/recording-retention (Twilio surface). Each is a
later phase; every surface ships behind its own Terraform flag.
