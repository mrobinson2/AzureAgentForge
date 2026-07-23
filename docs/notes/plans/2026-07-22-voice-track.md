# Voice track — implementation plan (option 1)

**Date:** 2026-07-22
**Status:** plan / not started
**Roadmap:** "Later" → A voice track: shared infrastructure (streaming STT,
low-latency TTS, VAD + barge-in, a persona overlay) that stays
provider-agnostic across Microsoft Voice Live and other commercial STT/TTS
providers, delivered over three surfaces (Discord voice, a web widget, a Twilio
phone line with a PIN gate, consent, and recording retention).
**Sizing:** multi-week greenfield (est. 6–8 weeks / 5 phases). Largest of the
three options — nothing real-time-voice exists yet. Ship each surface
flag-gated; default deploy stays voiceless.

## Where we are (what already exists)

- `apps/paperclip/auth-proxy.mjs` `POST /api/webhooks/voice/call-ended` — an
  **end-of-call intake** webhook (post-call structured payload → PaperClip
  issue), gated by `VOICE_WEBHOOK_SIGNING_SECRET`. This is NOT real-time voice;
  it is a batch handoff and stays as-is.
- No streaming STT, no TTS, no VAD/barge-in, no media transport. All greenfield.

## Architecture (provider-agnostic core + three surface adapters)

```
audio in ─▶ [VAD] ─▶ [streaming STT] ─▶ [persona overlay + agent turn] ─▶ [TTS] ─▶ audio out
                └──────── barge-in cancels in-flight TTS ────────┘
```

A single `services/voice-core/` owns the provider-agnostic pipeline behind a
narrow interface (`Transcriber`, `Synthesizer`, `VAD`, `PersonaOverlay`); each
surface (`discord` / `web` / `twilio`) is a thin transport adapter that feeds
audio frames in and plays frames out. Providers (Microsoft Voice Live, others)
are pluggable behind the STT/TTS interfaces — mirrors the model-router's
provider-agnostic posture and the sandbox/approval seam factory pattern.

## Phases

### Phase 1 — Voice-core pipeline, offline-testable (weeks 1–2)
- `services/voice-core/`: the interfaces + a `fake`/`local` provider (canned
  transcripts + silence TTS) so the whole turn loop is unit-testable with **no
  audio device and no cloud key**. VAD state machine (speech/silence/barge-in)
  pure + tested. Persona overlay = the existing agent-prompt persona applied to
  a turn.
- Wire one real provider (Microsoft Voice Live) behind the STT/TTS interface,
  isolated + marked **unverified** until a live spike (mirrors the `aca-job`
  sandbox posture).
- **Verify:** full turn loop (audio-in → transcript → agent turn → TTS frames)
  runs offline against the fake provider; barge-in cancels in-flight synthesis
  in a unit test.

### Phase 2 — Web-widget surface (weeks 2–3, ship first — no telco/Discord deps)
- A WebRTC/WebSocket audio bridge + a self-contained widget (`examples/` or a
  `services/voice-web/`). Simplest surface to demo; no external account.
- Consent banner + a visible recording indicator from day one.
- **Verify:** a browser can hold a spoken turn end-to-end against the real
  provider in a manual spike; the transport is offline-tested with a mock socket.

### Phase 3 — Twilio phone line (weeks 3–5, highest-compliance surface)
- Twilio Media Streams ↔ voice-core adapter (`services/voice-twilio/`).
- **PIN gate** before the agent engages, explicit **consent** prompt, and a
  **recording-retention** policy (retention window + deletion job). These are
  hard requirements, not add-ons — telco + recording is the compliance-heavy
  path.
- Secrets (Twilio auth, PIN) from Key Vault; ingress via the existing
  `cloudflare-tunnel` module.
- **Verify:** an inbound call without the PIN never reaches the agent; recordings
  are deleted after the retention window (job test); consent is logged.

### Phase 4 — Discord voice surface (weeks 5–6)
- Reuse the Discord voice primitives noted as present-but-unwired in the Hermes
  submodule (Opus/NaCl, RTP). `services/voice-discord/` adapter to voice-core.
- **Verify:** an in-channel voice session holds a turn; barge-in works over RTP.

### Phase 5 — Persona + ops hardening (weeks 6–8)
- Persona overlay per agent (voice_id/vibe already in AGENTS.md frontmatter —
  reuse `agent-frontmatter-schema`).
- Observability: per-turn latency spans (STT/agent/TTS) on the existing GenAI
  OTel pipeline; cost attribution per call via the model-router per-caller cap.
- Synthetic dogfooding canary (a scheduled canned call) — ties into the
  separate "synthetic dogfooding" roadmap item.
- **Verify:** turn-latency budget met on a real call; cost per call tracked;
  canary alerts on repeated failure.

## Cross-cutting

- **Flags:** each surface behind its own Terraform variable
  (`voice_web_enabled` / `voice_twilio_enabled` / `voice_discord_enabled`,
  default false). No surface deploys by default.
- **Provider-agnostic:** never import a provider SDK outside its interface
  adapter; the model-router's provider-detection discipline is the template.
- **Compliance:** consent + recording retention are gating requirements for the
  Twilio surface, not follow-ons. Legal review before the phone line goes live.

## Explicitly out of scope
- On-device / edge STT (Mac-edge Ollama path) — later.
- Multilingual voice — English first.
- Voice-driven infra actions (destroy gate etc.) — text approval only for now.

## Dependencies / ordering
Independent of options 2 and 3. Build web surface first (no external account),
Twilio last-but-highest-value. The end-of-call intake webhook is unrelated and
untouched.
