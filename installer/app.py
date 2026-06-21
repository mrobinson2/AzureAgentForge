"""Forge Console — local web GUI for turnkey AzureAgentForge deployment.

Run it:  ./forge        (or: python -m installer)
Then open the printed URL. Binds to 127.0.0.1 only.

Security model for a localhost tool:
  - Loopback bind only; never expose this on a network interface.
  - Every state-changing request must carry the per-session token that is
    embedded in the page URL at startup — a random browser tab on a
    malicious site can't fabricate requests to your console.
  - Cross-origin requests are rejected by an Origin allowlist.
  - apply / destroy additionally require typing the environment name.
  - apply is destroy-aware: if the saved plan would delete or replace any
    resource, it is blocked behind a second, distinct approval token.
"""

from __future__ import annotations

import secrets
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import core

app = FastAPI(title="Forge Console", docs_url=None, redoc_url=None)

SESSION_TOKEN = secrets.token_urlsafe(24)
HOST = "127.0.0.1"
PORT = 8321
STATIC = Path(__file__).parent / "static"

runner = core.Runner()
_last_config: dict = {}


def _guard(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
        raise HTTPException(403, "cross-origin request rejected")
    if request.headers.get("x-forge-token") != SESSION_TOKEN:
        raise HTTPException(401, "missing or invalid session token — reopen the printed URL")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/bootstrap")
def bootstrap(request: Request, token: str = "") -> dict:
    if token != SESSION_TOKEN:
        raise HTTPException(401, "open the console via the URL printed at startup")
    return {
        "token": SESSION_TOKEN,
        "profiles": list(core.VALID_PROFILES),
        "repo_root": str(core.REPO_ROOT),
        "config_written": bool(_last_config),
    }


@app.get("/api/checks")
def checks(request: Request) -> dict:
    _guard(request)
    return core.run_checks()


@app.get("/api/subscriptions")
def subscriptions(request: Request) -> list[dict]:
    _guard(request)
    return core.list_subscriptions()


class ConfigBody(BaseModel):
    subscription_id: str
    location: str = "eastus"
    environment: str = "dev"
    profile: str = "cost-optimized"
    telegram_enabled: bool = False
    discord_enabled: bool = False
    ai_foundry_endpoint: str = ""
    ai_foundry_deployment_id: str = ""
    image_tag: str = ""
    owner_email: str = ""
    keyvault_admin_object_ids: list[str] = []
    preview_only: bool = False


@app.post("/api/config")
def configure(body: ConfigBody, request: Request) -> dict:
    _guard(request)
    cfg = core.DeployConfig(**body.model_dump(exclude={"preview_only"}))
    errs = cfg.validate()
    if errs:
        raise HTTPException(422, "; ".join(errs))
    preview = core.render_tfvars(cfg)
    if body.preview_only:
        return {"preview": preview, "written": False}
    written = core.write_config(cfg)
    _last_config.clear()
    _last_config.update({"profile": cfg.profile, "environment": cfg.environment, **written})
    return {"preview": preview, "written": True, **written}


@app.get("/api/plan-summary")
def plan_summary(request: Request) -> dict:
    """Inspect the saved plan for destructive actions so the GUI can warn
    before apply. Reads the plan file only — never plans or applies."""
    _guard(request)
    if not _last_config:
        raise HTTPException(409, "write the configuration first (Configure tab)")
    try:
        return core.inspect_saved_plan(_last_config.get("profile", "cost-optimized"))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


class RunBody(BaseModel):
    step: str
    confirm: str = ""
    approve_destroy: str = ""


@app.post("/api/run")
def run_step(body: RunBody, request: Request) -> dict:
    _guard(request)
    step = body.step
    if step in core.NEEDS_CONFIG and not _last_config:
        raise HTTPException(409, "write the configuration first (Configure tab)")
    if step in core.DANGEROUS_STEPS and step != "compose-down":
        expected = _last_config.get("environment", "dev")
        if body.confirm != expected:
            raise HTTPException(
                428, f"type the environment name ('{expected}') to confirm '{step}'")
    # Destroy-aware gate: an apply whose saved plan deletes or replaces any
    # resource is blocked behind a second, distinct approval. The server
    # re-inspects the plan itself, so the gate can't be bypassed from the client.
    if step == "apply":
        try:
            summary = core.inspect_saved_plan(_last_config.get("profile", "cost-optimized"))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        if summary["has_destroy"] and body.approve_destroy != core.DESTROY_APPROVAL_TOKEN:
            raise HTTPException(409, {
                "error": "destroy_approval_required",
                "message": (f"this plan will DELETE or REPLACE {len(summary['destroyed'])} "
                            f"resource(s); type '{core.DESTROY_APPROVAL_TOKEN}' to approve"),
                "destroyed": summary["destroyed"],
                "approval_token": core.DESTROY_APPROVAL_TOKEN,
            })
    try:
        cmd = core.build_step_command(step, _last_config.get("profile", "cost-optimized"))
        run = runner.start(step, cmd)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"started": run.step}


@app.get("/api/state")
def state(request: Request) -> dict:
    _guard(request)
    return {**runner.state(), "config": dict(_last_config)}


@app.get("/api/stream")
def stream(request: Request, token: str = "") -> StreamingResponse:
    # EventSource can't set headers, so the SSE endpoint authenticates by token.
    if token != SESSION_TOKEN:
        raise HTTPException(401, "invalid token")

    def gen():
        for step, line in runner.subscribe():
            if line == "" and step == "":
                yield ": keepalive\n\n"
            elif line is None:
                yield f"event: stepend\ndata: {step}\n\n"
            else:
                payload = line.replace("\r", "")
                yield f"data: {payload}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


def main() -> None:
    import uvicorn
    url = f"http://{HOST}:{PORT}/?token={SESSION_TOKEN}"
    banner = (
        "\n  ╔══════════════════════════════════════════════════════════╗"
        "\n  ║  Forge Console — AzureAgentForge turnkey deployment       ║"
        "\n  ╚══════════════════════════════════════════════════════════╝"
        f"\n\n  Open:  {url}\n"
    )
    # flush — when stdout is piped (CI, logs) the banner must not sit in a buffer
    print(banner, flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
