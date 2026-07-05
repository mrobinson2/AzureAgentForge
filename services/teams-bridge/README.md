# Teams bridge

A small FastAPI service that bridges **Microsoft Teams** to the agent platform,
at parity with the [Discord plugin](../../integrations/discord/) and the
[Telegram gateway](../../integrations/telegram/): an inbound Teams message
becomes a PaperClip issue routed to the Orchestrator, and the agent's reply
returns to the channel as an Adaptive Card.

Disabled by default. Enable with the `teams_enabled` Terraform variable. See
[`integrations/teams/`](../../integrations/teams/) for the end-to-end setup.

## Endpoints

| Method | Path            | Purpose                                              |
|--------|-----------------|------------------------------------------------------|
| `GET`  | `/health`       | Liveness.                                            |
| `POST` | `/api/messages` | Bot Framework messaging endpoint. Non-`message` activities are acked and ignored; a `message` becomes a PaperClip issue. |

The endpoint **never returns 5xx** to Bot Framework (that triggers an aggressive
retry storm) — a downstream failure is acked with `{"queued": false}`.

## Configuration (env)

| Variable | Purpose |
|---|---|
| `PAPERCLIP_API_URL` | PaperClip base URL (default `http://paperclip:3000`). |
| `PAPERCLIP_COMPANY_ID` | Company the inbound issue is filed under. |
| `PAPERCLIP_API_KEY` | Bearer token for the PaperClip API (mounted from Key Vault). |
| `ORCHESTRATOR_AGENT_ID` | Optional — route Teams messages straight to one agent. |

## Security — read before enabling

The container's ingress is **internal** by design, so flipping `teams_enabled`
never publishes an unauthenticated message-ingest endpoint on its own. To take
it live you must:

1. **Expose `/api/messages`** to Azure Bot Service through the platform's
   Cloudflare tunnel (the same path PaperClip uses for public ingress).
2. **Add Bot Framework JWT validation** on `/api/messages`. This reference
   bridge trusts the activity body; a production deployment must validate the
   `Authorization` bearer token against the Bot Framework OpenID metadata
   before creating issues. This is the one hardening step that is intentionally
   left to the operator.

## Tests

```bash
pip install -r requirements-dev.txt
pytest            # 10 offline tests — pure helpers + the endpoint contract, no network
```
