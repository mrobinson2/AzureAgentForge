"""Tenant Console (P2) — FastAPI app, Forge Console pattern.

Security model (a localhost operator tool, same posture as AAF installer):
  - Loopback bind only (127.0.0.1); never expose on a network interface.
  - Every state-changing request must carry the per-session token embedded in
    the page URL at startup.
  - Cross-origin requests rejected by an Origin allowlist.
  - Provision/decommission are preview-first: nothing writes until the operator
    submits from the preview, and decommission additionally requires typing
    ``decommission <slug>`` (server-enforced — the executor re-checks it too).

The console constructs the API client from the operator's token (or mints one
from Key Vault like the CLI). ``create_app(client_factory=...)`` lets tests
inject a FakePlatform-backed client so the whole surface runs offline.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# Make `import tenantconsole` work from a plain checkout.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tenantconsole.client import ApiClient  # noqa: E402
from tenantconsole.contract import ContractError, load_contract  # noqa: E402
from tenantconsole.decommission import ConfirmationError, confirmation_phrase, run_decommission  # noqa: E402
from tenantconsole.jwt_mint import mint_jwt  # noqa: E402
from tenantconsole.playbook import load_pack, load_seed_memories, render_agent, validate_pack  # noqa: E402
from tenantconsole.steps import agent_env, agent_slug, run_provision  # noqa: E402

from console.runner import Runner  # noqa: E402

HOST = "127.0.0.1"
PORT = 8722
STATIC = Path(__file__).parent / "static"
DEFAULT_BASE_URL = "https://app.example.com"
KV_NAME = "aaf-dev-kv"
KV_SECRET = "platform-paperclip-automation-jwt-secret"


def _default_client_factory(base_url: str, token: str) -> ApiClient:
    return ApiClient(base_url, token)


def _resolve_token(token: str) -> str:
    if token:
        return token
    for env in ("TENANT_CONSOLE_TOKEN",):
        if os.environ.get(env):
            return os.environ[env]
    secret = os.environ.get("TENANT_CONSOLE_JWT_SECRET") or _kv_secret()
    if not secret:
        raise HTTPException(
            401,
            "no API token — paste an admin token, set $TENANT_CONSOLE_TOKEN, or "
            f"ensure `az` can read {KV_SECRET} from {KV_NAME}",
        )
    return mint_jwt(secret, sub="tenant-console", role="admin", ttl_seconds=3600)


def _kv_secret() -> Optional[str]:
    try:
        out = subprocess.run(
            ["az", "keyvault", "secret", "show", "--vault-name", KV_NAME,
             "--name", KV_SECRET, "--query", "value", "-o", "tsv"],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _load_contract_yaml(yaml_text: str):
    """Parse posted contract YAML by writing it to a temp file (loader wants a path)."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
        fh.write(yaml_text)
        tmp = fh.name
    try:
        return load_contract(tmp)
    finally:
        os.unlink(tmp)


def _plan(contract, pack) -> Dict[str, Any]:
    """The preview plan — pure render, zero network."""
    variables = contract.render_variables()
    env = agent_env(contract)
    agents = []
    for spec in pack.agents:
        try:
            rendered = render_agent(pack, spec.role, variables)
            note = f"{len(rendered)} bytes"
            err = None
        except Exception as exc:  # noqa: BLE001
            note, err = None, str(exc)
        agents.append({
            "slug": agent_slug(spec.slug_prefix, contract.slug),
            "band": spec.band, "model": spec.model, "role": spec.role,
            "reports_to": spec.reports_to, "instructions": note, "error": err,
        })
    try:
        seeds = load_seed_memories(pack, variables)
        seed_count, seed_err = len(seeds), None
    except Exception as exc:  # noqa: BLE001
        seed_count, seed_err = 0, str(exc)
    return {
        "slug": contract.slug, "display_name": contract.display_name,
        "vertical": contract.vertical, "workspace": contract.workspace,
        "budgets": {"daily_usd": contract.daily_usd, "per_run_usd": contract.per_run_usd,
                    "monthly_cents": contract.monthly_budget_cents()},
        "agents": agents, "agent_env": env,
        "channels": [c.kind for c in contract.channels],
        "seed_count": seed_count, "seed_error": seed_err,
        "pack_problems": validate_pack(pack),
    }


class ContractBody(BaseModel):
    contract_yaml: str
    base_url: str = DEFAULT_BASE_URL
    token: str = ""


class ProvisionBody(ContractBody):
    skip_smoke: bool = False


class DecommissionBody(ContractBody):
    confirm: str = ""


def create_app(client_factory: Optional[Callable[[str, str], ApiClient]] = None) -> FastAPI:
    app = FastAPI(title="Tenant Console", docs_url=None, redoc_url=None)
    # aaf-0019: reject requests whose Host header isn't loopback — defence in
    # depth against DNS-rebinding that would let a remote page reach this
    # localhost-bound console.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.state.session_token = secrets.token_urlsafe(24)
    app.state.runner = Runner()
    factory = client_factory or _default_client_factory

    _ALLOWED_ORIGINS = (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}")

    def guard(request: Request) -> None:
        origin = request.headers.get("origin")
        # aaf-0019: on state-changing methods a MISSING Origin is a FAILED check,
        # not a pass. A browser always sends Origin on cross-site POSTs, so an
        # absent one on a mutating request is treated as untrusted.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if origin is None or origin not in _ALLOWED_ORIGINS:
                raise HTTPException(403, "cross-origin or origin-less request rejected")
        elif origin and origin not in _ALLOWED_ORIGINS:
            raise HTTPException(403, "cross-origin request rejected")
        if request.headers.get("x-console-token") != app.state.session_token:
            raise HTTPException(401, "missing or invalid session token — reopen the printed URL")

    def load(body: ContractBody):
        try:
            contract = _load_contract_yaml(body.contract_yaml)
        except (ContractError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc))
        return contract, load_pack(contract.pack_dir)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/bootstrap")
    def bootstrap(token: str = "") -> dict:
        if token != app.state.session_token:
            raise HTTPException(401, "open the console via the URL printed at startup")
        return {"token": app.state.session_token, "default_base_url": DEFAULT_BASE_URL}

    @app.post("/api/preview")
    def preview(body: ContractBody, request: Request) -> dict:
        """Preview-first: render the plan. Zero write calls, no auth needed."""
        guard(request)
        contract, pack = load(body)
        return _plan(contract, pack)

    @app.post("/api/preflight")
    def preflight(body: ContractBody, request: Request) -> dict:
        """Validate the contract/pack and (if a token is given) probe the stack."""
        guard(request)
        contract, pack = load(body)
        checks = {
            "contract_valid": True,
            "pack_valid": not validate_pack(pack),
            "stack_reachable": None,
        }
        if body.token or os.environ.get("TENANT_CONSOLE_TOKEN"):
            try:
                client = factory(body.base_url, _resolve_token(body.token))
                client.whoami()
                checks["stack_reachable"] = True
            except Exception as exc:  # noqa: BLE001
                checks["stack_reachable"] = False
                checks["stack_error"] = str(exc)
        return {"workspace": contract.workspace, "checks": checks}

    @app.post("/api/provision")
    def provision(body: ProvisionBody, request: Request) -> dict:
        guard(request)
        contract, pack = load(body)
        client = factory(body.base_url, _resolve_token(body.token))

        def job(on_event):
            return run_provision(contract, pack, client,
                                 skip_smoke=body.skip_smoke, on_event=on_event)

        try:
            app.state.runner.start("provision", contract.slug, job)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"started": True, "tenant": contract.slug}

    @app.post("/api/decommission")
    def decommission(body: DecommissionBody, request: Request) -> dict:
        guard(request)
        contract, _ = load(body)
        expected = confirmation_phrase(contract.slug)
        if body.confirm != expected:
            raise HTTPException(428, f"type '{expected}' to confirm decommission")
        client = factory(body.base_url, _resolve_token(body.token))

        def job(on_event):
            return run_decommission(contract, client, body.confirm, on_event=on_event)

        try:
            app.state.runner.start("decommission", contract.slug, job)
        except (RuntimeError, ConfirmationError) as exc:
            raise HTTPException(409, str(exc))
        return {"started": True, "tenant": contract.slug}

    @app.get("/api/state")
    def state(request: Request) -> dict:
        guard(request)
        s = app.state.runner.state
        return {"kind": s.kind, "tenant": s.tenant, "running": s.running,
                "outcome": s.outcome, "steps": s.steps, "report": s.report, "error": s.error}

    @app.get("/api/stream")
    def stream(token: str = "") -> StreamingResponse:
        if token != app.state.session_token:
            raise HTTPException(401, "invalid token")

        def gen():
            import json
            for evt in app.state.runner.subscribe():
                yield f"data: {json.dumps(evt)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    return app


app = create_app()


def main() -> None:
    import uvicorn
    token = app.state.session_token
    url = f"http://{HOST}:{PORT}/?token={token}"
    print(
        "\n  ╔══════════════════════════════════════════════════════════╗"
        "\n  ║  Tenant Console — AzureAgentForge tenant provisioning     ║"
        "\n  ╚══════════════════════════════════════════════════════════╝"
        f"\n\n  Open:  {url}\n",
        flush=True,
    )
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
