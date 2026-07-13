# Design — Real-time voice web app (Microsoft Voice Live + Azure Speech)

**Date:** 2026-06-25
**Status:** Design only — *not* scheduled for build. Drafted as the announced
direction for first-class voice; the implementation is a follow-on milestone.
**Target:** AzureAgentForge as a flag-gated **template feature**
(`voice_enabled`), with the operator's own platform as the reference deployment.

---

## 1. Summary

A browser-based, low-latency **conversational voice surface**: you open a web
page, talk to your agents, and hear them answer — every hop staying inside one
Azure region/VNet so the round-trip is short. Microsoft **Voice Live** (BYOM
mode) is the ear (speech → text); **Azure AI Speech** neural TTS is the mouth
(text → speech); the agent stack (Hermes / model-router / PaperClip memory) is
the brain. PaperClip stays in the loop for **audit and replay**, but off the
hot path so it never adds conversational latency.

This eliminates the latency and turn-taking limits of a chat-app bridge
(Telegram/Discord) by owning the transport end to end.

## 2. Goals / non-goals

**Goals**
- Push-to-talk (or VAD-gated) **half-duplex** voice conversation in a browser.
- Single agent persona per session, backed by the real agent stack.
- All inference and speech processing stays in-region/in-VNet for low latency.
- Durable, human-readable **transcript per session** + PaperClip issue for audit.
- Ship as a **sanitized, generic AAF template** any operator can enable.

**Non-goals (this milestone)**
- Full-duplex barge-in / interrupt-mid-sentence. (Half-duplex turn-taking only.)
- **Multiple agents speaking** in one session. (Single persona; multi-agent
  voice is a separate, larger initiative.)
- Voice Live's *own* model generating responses (we use BYOM — Voice Live does
  STT only; the agent stack is the brain).
- Mobile-native apps. (Responsive web only.)

## 3. What exists today (grounding)

- **Voice Live BYOM STT is proven.** A spike (2026-05-31, `gpt-realtime-mini`,
  api-version `2026-01-01-preview`) confirmed: open a session, stream PCM16
  24 kHz, suppress the model (`modalities:["text"]`, `turn_detection:null`, no
  `response.create`), and receive a clean transcript via
  `conversation.item.input_audio_transcription.completed` at **~841 ms** for a
  4.2 s utterance. This is the canonical STT path.
- **A provider abstraction already exists** (`paperclip-plugin-discord`,
  `feat/voice-provider-abstraction`, ~1,136 LOC): an `AzureVoiceLiveProvider`
  (STT), a Deepgram fallback, Opus→PCM resampling, and dual transcript sinks.
  The provider/event model is reusable; the Discord transport is not.
- **Hermes has no synchronous "message → reply" endpoint.** Its web server is
  management/dashboard routes plus a pty bridge to the TUI; the agent loop runs
  through the async **gateway** pipeline. A synchronous dispatch seam is
  net-new code (see §6).
- **The model-router is a synchronous, in-VNet LLM gateway** (OpenAI-compatible)
  — the fastest existing brain, but without tools/memory/persona on its own.
- **TTS (voice out) is unbuilt.** The STT spike explicitly deferred it. This
  design selects **Azure AI Speech neural TTS** (GA, same region) for output.
- An **auth-proxy** already fronts the platform's services — reuse it for the
  web app's auth rather than inventing a new scheme.

## 4. Architecture

```
 Browser (React, App Service, behind Cloudflare tunnel + auth-proxy)
   │  mic capture (PCM16) over WSS
   ▼
 voice-relay  (new backend service — holds ALL keys; the trust boundary)
   ├─ proxies/streams audio → Voice Live (BYOM STT) ──► transcript
   ├─ dispatches transcript → agent brain (§6) ──────► reply text
   ├─ reply text → Azure Speech neural TTS ──────────► audio
   │                                  (streamed back to the browser to play)
   └─ on session end: write transcript .md → Blob;  file/append PaperClip issue
```

**Components (each independently testable):**

1. **`voice-web`** — React SPA. Mic capture, WSS to the relay, playback,
   push-to-talk / VAD UI, session lifecycle. Holds **no secrets**.
2. **`voice-relay`** — the only component with credentials. Token-broker +
   audio/dispatch orchestrator. FastAPI, mirrors the `teams-bridge` service
   shape (own Container App, flag-gated). This is the security boundary (§5).
3. **STT adapter** — Voice Live BYOM client (port the proven
   `AzureVoiceLiveProvider` event model; drop the Discord transport).
4. **Brain dispatch** — pluggable seam (§6): `router` (fast MVP) or `hermes`
   (full agent). Chosen by config so the surface doesn't change when the brain
   is upgraded.
5. **TTS adapter** — Azure AI Speech neural TTS, streamed.
6. **Transcript/audit writer** — markdown → Blob + PaperClip issue (§7).

## 5. Security — the relay is non-negotiable

The browser must **never** hold the Voice Live or Speech keys. The `voice-relay`
holds them (from Key Vault, fetched at startup) and either proxies the Voice
Live WebSocket or mints **short-lived tokens**. The SPA authenticates through
the existing **auth-proxy**; the relay authorizes every session before opening
any upstream connection. Audio is content-redacted in logs; transcripts are
treated as user content and stored only in the operator's Blob + PaperClip.

## 6. Brain dispatch (the load-bearing decision)

Because Hermes exposes no synchronous reply endpoint, the brain is a **pluggable
seam** with two implementations:

- **`router` (MVP / fast path).** Relay calls the model-router
  (`/v1/chat/completions`) with a single-agent system prompt. Lowest latency,
  uses only existing infrastructure, no tools/memory. Good enough to prove the
  loop and demo the conversational feel.
- **`hermes` (full vision).** A **new synchronous dispatch endpoint** added to
  Hermes's gateway (in AAF: a build-time `patch-*.mjs`, the established
  mechanism for Hermes changes) that runs one agent turn with tools + Honcho
  memory + persona and returns the reply text. Higher value, real new code in an
  upstream-vendored component — the milestone's main engineering risk.

The relay treats both behind one interface; swapping `router`→`hermes` changes
config, not the web app.

## 7. Transcript & audit (off the hot path)

Each session produces a **markdown transcript** (frontmatter + turn-by-turn) the
relay writes to **Blob storage** at session end. PaperClip audit is then either:

- **Direct (recommended):** the relay files/updates **one PaperClip issue per
  session** at session end — synchronous with the session, no extra moving
  parts; or
- **Backfill (original vision):** a separate **ACA scheduled Job** (every 5 min)
  scans Blob for new transcripts and backfills PaperClip issues — decouples
  audit from the live path at the cost of one more deployable.

Default to **direct**; keep the Blob markdown as the durable artifact (it also
dovetails with the v1.3 Obsidian memory interface — transcripts can live in the
vault). The markdown→issue mapping is identical either way, so we can add the
cron later without reworking the writer.

## 8. AAF template & sanitization

Shipped generic and flag-gated (`voice_enabled`, default false):
- No hardcoded guild/channel/webhook IDs, agent UUIDs, tenant names, or
  Cloudflare specifics — all config/Terraform variables.
- Terraform: the App Service (or Container App) for `voice-web`, the
  `voice-relay` Container App, the Azure Speech + Voice Live resources, KV
  secrets, and ingress wiring — all `count`-gated on `voice_enabled`.
- Reference deployment (operator's platform) documented separately; the public
  template carries only generic defaults.

## 9. Phasing

1. **Spike (½ day):** browser→relay→Voice Live STT round-trip + Azure Speech
   TTS playback, one real agent turn via `router`. Confirms latency + the relay
   token model end to end.
2. **MVP slice:** `voice-web` + `voice-relay` + `router` brain + direct
   PaperClip issue + Blob transcript. Dev only.
3. **Full vision:** `hermes` dispatch endpoint (tools/memory/persona),
   optional cron backfill, sanitized AAF template, then production.

## 10. Latency budget (half-duplex turn)

| Stage | Est. |
|---|---|
| Capture → relay (end-of-utterance) | ~100–200 ms |
| Voice Live STT (commit → transcript) | ~0.8 s (measured) |
| Agent reply (`router`; `hermes` higher) | ~1–3 s |
| Azure Speech TTS (first audio) | ~0.3–0.7 s |
| **Perceived round-trip** | **~2.5–5 s/turn** |

Voice-assistant feel (turn-taking), **not** interrupt-mid-sentence full duplex.
Set this expectation in the UI (clear "speaking / listening / thinking" states).

## 11. Testing

- STT adapter: event-model unit tests against recorded Voice Live event logs.
- Relay: auth/authorization, token brokering, no-key-leak assertions, session
  lifecycle, error/reconnect.
- TTS adapter: synthesis + streaming offline tests.
- Transcript writer: markdown render + PaperClip mapping (offline).
- E2E: scripted audio → transcript → reply → audio, latency assertion.

## 12. Risks & open questions

- **Hermes dispatch endpoint** is the biggest unknown — scope/stability of a
  new synchronous seam in the gateway. Mitigated by shipping `router` first.
- **Voice Live session stability** over long/real sessions (spike ran seconds):
  reconnect/keepalive strategy TBD.
- **Real mic audio** (vs synthetic) may affect STT quality/latency.
- **Managed-identity auth** to Voice Live (spike used API key) — verify the WS
  upgrade auth pattern under MI.
- **Cost**: per-minute Voice Live + Speech spend; add a budget alert.
- **Barge-in**: deferred; would require full-duplex streaming + VAD arbitration.

## 13. References

- STT spike findings: `voice-live-byom/FINDINGS.md` (reference platform).
- Provider abstraction: `paperclip-plugin-discord` `feat/voice-provider-abstraction`.
- v1.3 Obsidian memory interface (transcript-as-vault synergy).
- `teams-bridge` (service shape to mirror for `voice-relay`).
