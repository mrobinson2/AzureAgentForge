# Slack bridge

A small FastAPI service that bridges **Slack** to the agent platform, at parity
with the [Discord plugin](../../integrations/discord/), the
[Telegram gateway](../../integrations/telegram/), and the
[Teams bridge](../teams-bridge/): an inbound Slack message becomes a PaperClip
issue routed to the Orchestrator, and the agent's reply returns to the channel
via `chat.postMessage`.

Disabled by default. Enable with the `slack_enabled` Terraform variable. See
[`integrations/slack/`](../../integrations/slack/) for the end-to-end setup.

## Endpoints

| Method | Path             | Purpose                                                                 |
|--------|------------------|-------------------------------------------------------------------------|
| `GET`  | `/health`        | Liveness.                                                               |
| `POST` | `/slack/events`  | Slack Events API endpoint. Answers the `url_verification` challenge; a `message` event becomes a PaperClip issue; non-`message`/bot/subtyped events are acked and ignored. |

The endpoint **never returns 5xx** to Slack (that triggers an aggressive retry
storm) — a downstream failure is acked with `{"queued": false}`.

## Configuration (env)

| Variable | Purpose |
|---|---|
| `PAPERCLIP_API_URL` | PaperClip base URL (default `http://paperclip:3000`). |
| `PAPERCLIP_COMPANY_ID` | Company the inbound issue is filed under. |
| `PAPERCLIP_API_KEY` | Bearer token for the PaperClip API (mounted from Key Vault). |
| `ORCHESTRATOR_AGENT_ID` | Optional — route Slack messages straight to one agent. |
| `SLACK_SIGNING_SECRET` | Slack app signing secret — HMAC-verifies inbound requests (mounted from Key Vault). |
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-…`) for `chat.postMessage` replies (mounted from Key Vault). |
| `SLACK_REPLAY_WINDOW_SECONDS` | Replay window for the request timestamp (default `300`). |

## Security — read before enabling

The container's ingress is **internal** by design, so flipping `slack_enabled`
never publishes an unauthenticated event-ingest endpoint on its own. To take it
live you must:

1. **Expose `/slack/events`** to Slack through the platform's Cloudflare tunnel
   (the same path PaperClip uses for public ingress).
2. **Set `SLACK_SIGNING_SECRET`** so the bridge HMAC-verifies the
   `X-Slack-Signature` on every request. With it unset the endpoint logs a
   warning and trusts the body — local/dev only.

## Tests

```bash
pip install -r requirements-dev.txt
pytest            # 20 offline tests — pure helpers, HMAC verification, the endpoint contract, no network
```
