"""Operator morning brief -> chat webhook + stdout.

Answers the README's founding operator question — "what happened overnight,
and which model made which decision at what cost?" — in ONE message composed
from sources that already exist but were never joined:

  runs       PaperClip  GET /api/companies/{id}/runs?sinceMinutes=N
  router     model-router GET /health (unauthenticated: over-budget tiers,
             breaker state, kill switch) and, when ROUTER_API_KEY is set,
             GET /debug/flight-recorder (spend + outcomes by caller and tier)
  approvals  governor   GET /escalation-sla?window_days=1
  memory     governor   GET /digest?window_hours=N

Runs as a scheduled job off the memory-governor image
(``python -m governor.morning_brief``), mirroring ``digest_post``. Every
source is optional: a source that is down produces a section marked
unavailable, never an empty or failed brief. The brief is ALWAYS printed to
stdout (so Log Analytics has it even with no chat target); it is also POSTed
when ``BRIEF_WEBHOOK_URL`` is set. Stdlib only — no new image dependency.

Env:
  GOVERNOR_BASE_URL                 e.g. http://ca-memory-governor-dev
  GOVERNOR_API_KEY                  X-Governor-Key
  ROUTER_BASE_URL                   e.g. http://ca-hermes-dev (router sidecar
                                    behind the hermes ingress); default
                                    http://localhost:8080
  ROUTER_API_KEY                    optional; enables the flight-recorder
                                    spend/outcome section
  PAPERCLIP_BASE_URL                e.g. http://ca-paperclip-dev
  PAPERCLIP_COMPANY_ID              company whose runs to summarize
  PAPERCLIP_AUTOMATION_JWT_SECRET   HS256 secret the auth-proxy verifies
  BRIEF_WEBHOOK_URL                 Slack / Discord / generic incoming webhook
                                    (unset -> stdout only, exit 0)
  BRIEF_WINDOW_HOURS                window (default 24, clamped 1..168)
  BRIEF_TITLE                       heading prefix (default "AzureAgentForge")
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

# Slack blocks text and Discord content both cap near 4000 chars; keep headroom.
MAX_CHARS = 3900
TOP_N = 3

SUCCESS_STATUSES = frozenset({"completed", "complete", "succeeded", "success", "done"})
FAILED_STATUSES = frozenset(
    {"failed", "failure", "error", "errored", "cancelled", "canceled", "timeout", "timed_out", "aborted"}
)

# Must match the auth-proxy's expected claims (paperclip.tf sets the issuer /
# audience env on the PaperClip app; the watchdog uses the same defaults).
JWT_SUB = os.getenv("BRIEF_JWT_SUB", "morning-brief")
JWT_SCOPE = ["issues:read", "agents:read"]
JWT_ISSUER = os.getenv("PAPERCLIP_AUTOMATION_JWT_ISSUER", "automation-agent")
JWT_AUDIENCE = os.getenv("PAPERCLIP_AUTOMATION_JWT_AUDIENCE", "paperclip-api")


# ── pure helpers ─────────────────────────────────────────────────────────────


def _clamp_window(raw: str | None) -> int:
    try:
        return max(1, min(168, int(raw)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 24


def _num(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _duration_s(run: dict) -> float | None:
    a, b = _parse_dt(run.get("startedAt")), _parse_dt(run.get("finishedAt"))
    if a and b and b >= a:
        return (b - a).total_seconds()
    return None


def _status_bucket(status: Any) -> str:
    s = str(status or "").strip().lower()
    if s in SUCCESS_STATUSES:
        return "completed"
    if s in FAILED_STATUSES:
        return "failed"
    return "running"


def summarize_runs(runs: list[dict] | None) -> dict:
    """Pure: PaperClip run rows -> per-agent outcome counts, spend, top lists."""
    per_agent: dict[str, dict[str, int]] = {}
    totals = {"completed": 0, "failed": 0, "running": 0}
    spend = 0.0
    scored: list[dict] = []
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        agent = str(run.get("agentName") or run.get("agentId") or "unknown")
        bucket = _status_bucket(run.get("status"))
        per_agent.setdefault(agent, {"completed": 0, "failed": 0, "running": 0})[bucket] += 1
        totals[bucket] += 1
        cost = _num(run.get("cost_usd", run.get("costUsd")))
        spend += cost
        scored.append(
            {
                "id": str(run.get("id") or ""),
                "agent": agent,
                "issue": str(run.get("issueId") or run.get("issue_id") or ""),
                "cost_usd": round(cost, 4),
                "duration_s": _duration_s(run),
                "status": bucket,
            }
        )
    costliest = sorted(scored, key=lambda r: r["cost_usd"], reverse=True)[:TOP_N]
    slowest = sorted(
        (r for r in scored if r["duration_s"] is not None),
        key=lambda r: r["duration_s"],
        reverse=True,
    )[:TOP_N]
    return {
        "per_agent": per_agent,
        "totals": totals,
        "spend_usd": round(spend, 2),
        "costliest": [r for r in costliest if r["cost_usd"] > 0],
        "slowest": slowest,
    }


def _kill_switch_engaged(ks: Any) -> bool:
    if not isinstance(ks, dict):
        return False
    engaged = ks.get("engaged")
    if isinstance(engaged, bool):
        return engaged
    return bool(engaged)


def summarize_router(health: dict | None, recorder: dict | None) -> dict:
    """Pure: router /health (+ optional /debug/flight-recorder) -> guardrail view."""
    health = health if isinstance(health, dict) else {}
    tiers = health.get("tiers") if isinstance(health.get("tiers"), dict) else {}
    over = sorted(t for t, v in tiers.items() if isinstance(v, dict) and v.get("over_budget"))
    cb = health.get("circuit_breakers") if isinstance(health.get("circuit_breakers"), dict) else {}
    by_tier = cb.get("by_tier") if isinstance(cb.get("by_tier"), dict) else {}
    open_tiers = sorted(
        t for t, st in by_tier.items()
        if str(st.get("state") if isinstance(st, dict) else st).lower() in {"open", "half_open"}
    )
    out: dict[str, Any] = {
        "over_budget_tiers": over,
        "open_breaker_tiers": open_tiers,
        "kill_switch_engaged": _kill_switch_engaged(health.get("kill_switch")),
        "kill_switch_scopes": list((health.get("kill_switch") or {}).get("engaged") or [])
        if isinstance(health.get("kill_switch"), dict) else [],
        "calls": None,
        "errors": None,
        "waste_breaker_hits": None,
        "spend_by_caller": {},
        "spend_by_tier": {},
    }
    if isinstance(recorder, dict) and isinstance(recorder.get("recent"), list):
        calls = errors = hits = 0
        by_caller: dict[str, float] = {}
        by_tier_spend: dict[str, float] = {}
        for ev in recorder["recent"]:
            if not isinstance(ev, dict):
                continue
            calls += 1
            if str(ev.get("outcome") or "").lower() not in {"success", "ok"}:
                errors += 1
            if ev.get("breaker_verdicts"):
                hits += 1
            cost = _num(ev.get("cost_usd"))
            caller = str(ev.get("caller") or "unknown")
            tier = str(ev.get("served_tier") or ev.get("requested_model") or "unknown")
            by_caller[caller] = by_caller.get(caller, 0.0) + cost
            by_tier_spend[tier] = by_tier_spend.get(tier, 0.0) + cost
        out.update(
            calls=calls,
            errors=errors,
            waste_breaker_hits=hits,
            spend_by_caller={k: round(v, 4) for k, v in sorted(by_caller.items(), key=lambda kv: -kv[1])},
            spend_by_tier={k: round(v, 4) for k, v in sorted(by_tier_spend.items(), key=lambda kv: -kv[1])},
        )
    return out


def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 90:
        return f"{int(seconds)}s"
    return f"{seconds / 60:.0f}m"


def render(
    *,
    window_hours: int,
    now: datetime,
    runs: dict | None,
    router: dict | None,
    approvals: dict | None,
    memory: dict | None,
    errors: dict[str, str],
    title: str = "AzureAgentForge",
) -> str:
    """Pure: assembled sections -> one chat-sized Markdown-ish message."""
    lines: list[str] = [f"☀️ **{title} — Morning brief** · last {window_hours}h · {now.strftime('%Y-%m-%d %H:%M UTC')}"]

    # Runs
    lines.append("")
    lines.append("**Runs**")
    if runs is None:
        lines.append(f"  unavailable ({errors.get('runs', 'no data')})")
    else:
        t = runs["totals"]
        lines.append(
            f"  {t['completed']} completed · {t['failed']} failed · {t['running']} running · spend {_fmt_usd(runs['spend_usd'])}"
        )
        for agent, c in sorted(runs["per_agent"].items(), key=lambda kv: -(kv[1]["completed"] + kv[1]["failed"] + kv[1]["running"])):
            flag = " ⚠️" if c["failed"] else ""
            lines.append(f"  • {agent}: {c['completed']}✓ {c['failed']}✗ {c['running']}…{flag}")
        if runs["costliest"]:
            lines.append("  costliest: " + ", ".join(
                f"{r['agent']} {_fmt_usd(r['cost_usd'])}" + (f" (issue {r['issue']})" if r["issue"] else "")
                for r in runs["costliest"]
            ))
        if runs["slowest"]:
            lines.append("  slowest: " + ", ".join(
                f"{r['agent']} {_fmt_dur(r['duration_s'])}" for r in runs["slowest"]
            ))

    # Guardrails / spend
    lines.append("")
    lines.append("**Guardrails**")
    if router is None:
        lines.append(f"  unavailable ({errors.get('router', 'no data')})")
    else:
        trips: list[str] = []
        if router["over_budget_tiers"]:
            trips.append("over budget: " + ", ".join(router["over_budget_tiers"]))
        if router["open_breaker_tiers"]:
            trips.append("breaker open: " + ", ".join(router["open_breaker_tiers"]))
        if router["kill_switch_engaged"]:
            trips.append("kill switch ENGAGED: " + ", ".join(router.get("kill_switch_scopes") or ["?"]))
        if router.get("waste_breaker_hits"):
            trips.append(f"waste-breaker hits: {router['waste_breaker_hits']}")
        lines.append("  " + ("; ".join(trips) if trips else "no guardrail trips"))
        if router["calls"] is not None:
            lines.append(f"  router calls {router['calls']} · errors {router['errors']}")
            if router["spend_by_tier"]:
                lines.append("  spend by tier: " + ", ".join(
                    f"{k} {_fmt_usd(v)}" for k, v in list(router["spend_by_tier"].items())[:6]
                ))
            if router["spend_by_caller"]:
                lines.append("  spend by agent: " + ", ".join(
                    f"{k} {_fmt_usd(v)}" for k, v in list(router["spend_by_caller"].items())[:8]
                ))

    # Approvals
    lines.append("")
    lines.append("**Approvals**")
    if approvals is None:
        lines.append(f"  unavailable ({errors.get('approvals', 'no data')})")
    else:
        lines.append("  " + _approvals_line(approvals))

    # Memory
    lines.append("")
    lines.append("**Memory**")
    if memory is None:
        lines.append(f"  unavailable ({errors.get('memory', 'no data')})")
    else:
        text = str(memory.get("text") or "").strip() or "no digest text"
        lines.append("  " + text.splitlines()[0])

    body = "\n".join(lines)
    if len(body) > MAX_CHARS:
        body = body[: MAX_CHARS - 1] + "…"
    return body


def _approvals_line(approvals: dict) -> str:
    """One line from the governor's /escalation-sla rollup. Structured fields
    first (total/unacked/breaches/expired); fall back to the report text's
    first informative line; then the honest zero."""
    total = approvals.get("total")
    if isinstance(total, int):
        unacked = int(approvals.get("unacked") or 0)
        breaches = int(approvals.get("breaches") or 0)
        expired = int(approvals.get("expired") or 0)
        if total == 0:
            return "nothing waiting on a human (no escalations in window)"
        parts = [f"{total} opened", f"{unacked} unacked"]
        if breaches:
            parts.append(f"{breaches} SLA breached ⚠️")
        if expired:
            parts.append(f"{expired} expired")
        warnings = approvals.get("sla_warnings") or []
        if warnings:
            parts.append(f"{len(warnings)} warning(s)")
        return " · ".join(parts)
    text = str(approvals.get("text") or "").strip()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("⏱", "**", "#")):
            return line
    if text:
        return text.splitlines()[0].strip()
    unacked = approvals.get("unacked")
    return "nothing waiting on a human" if unacked in (0, None) else f"{unacked} waiting on a human"


def build_payload(text: str, webhook_url: str) -> dict:
    """Slack incoming webhooks want {"text"}; Discord wants {"content"}; an
    unknown target gets both so either style of receiver renders it."""
    host = webhook_url.lower()
    if "slack.com" in host:
        return {"text": text}
    if "discord" in host:
        return {"content": text}
    return {"content": text, "text": text}


# ── I/O ──────────────────────────────────────────────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_jwt(secret: str, *, ttl_s: int = 300) -> str:
    """Minimal HS256 automation JWT matching the auth-proxy's verifyJwt
    (mirrors watchdog.mint_jwt / governor.scope_watcher.mint_jwt)."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps(
            {
                "sub": JWT_SUB,
                "role": "automation",
                "scope": JWT_SCOPE,
                "iss": JWT_ISSUER,
                "aud": JWT_AUDIENCE,
                "iat": now,
                "exp": now + ttl_s,
            }
        ).encode()
    )
    sig = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def fetch_json(url: str, headers: dict | None = None, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed in-mesh URLs
        return json.loads(resp.read().decode("utf-8") or "null")


def post_webhook(webhook_url: str, payload: dict) -> int:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        return resp.status


def _try(name: str, fn: Callable[[], Any], errors: dict[str, str]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — one dead source must not kill the brief
        errors[name] = f"{type(exc).__name__}: {exc}"[:120]
        print(f"morning_brief: {name} unavailable: {exc}", file=sys.stderr, flush=True)
        return None


def collect() -> tuple[dict[str, Any], dict[str, str]]:
    """Fetch every source independently; return (sections, errors)."""
    errors: dict[str, str] = {}
    window_h = _clamp_window(os.environ.get("BRIEF_WINDOW_HOURS", "24"))

    gov = os.environ.get("GOVERNOR_BASE_URL", "http://localhost:8090").rstrip("/")
    gov_headers = {"X-Governor-Key": os.environ.get("GOVERNOR_API_KEY", "")}
    router = os.environ.get("ROUTER_BASE_URL", "http://localhost:8080").rstrip("/")
    router_key = os.environ.get("ROUTER_API_KEY", "").strip()
    pc = os.environ.get("PAPERCLIP_BASE_URL", "").rstrip("/")
    company = os.environ.get("PAPERCLIP_COMPANY_ID", "").strip()
    jwt_secret = os.environ.get("PAPERCLIP_AUTOMATION_JWT_SECRET", "").strip()

    def runs_fn():
        if not (pc and company and jwt_secret):
            raise RuntimeError("PAPERCLIP_BASE_URL / PAPERCLIP_COMPANY_ID / JWT secret not configured")
        url = f"{pc}/api/companies/{company}/runs?sinceMinutes={window_h * 60}&limit=200"
        data = fetch_json(url, {"Authorization": f"Bearer {mint_jwt(jwt_secret)}", "Origin": pc}, timeout=30)
        rows = data if isinstance(data, list) else (data or {}).get("runs", [])
        return summarize_runs(rows)

    def router_fn():
        health = fetch_json(f"{router}/health")
        recorder = None
        if router_key:
            try:
                recorder = fetch_json(
                    f"{router}/debug/flight-recorder?limit=500",
                    {"Authorization": f"Bearer {router_key}"},
                )
            except urllib.error.HTTPError as exc:
                why = "disabled (FLIGHT_RECORDER_ENABLED=false)" if exc.code == 404 else f"HTTP {exc.code}"
                print(f"morning_brief: flight recorder {why}", file=sys.stderr, flush=True)
            except Exception as exc:  # noqa: BLE001 — recorder is optional detail
                print(f"morning_brief: flight recorder unavailable: {exc}", file=sys.stderr, flush=True)
        return summarize_router(health, recorder)

    def approvals_fn():
        return fetch_json(f"{gov}/escalation-sla?window_days={max(1, round(window_h / 24))}", gov_headers)

    def memory_fn():
        return fetch_json(f"{gov}/digest?window_hours={window_h}", gov_headers)

    sections = {
        "runs": _try("runs", runs_fn, errors),
        "router": _try("router", router_fn, errors),
        "approvals": _try("approvals", approvals_fn, errors),
        "memory": _try("memory", memory_fn, errors),
    }
    return sections, errors


def main() -> int:
    window_h = _clamp_window(os.environ.get("BRIEF_WINDOW_HOURS", "24"))
    title = os.environ.get("BRIEF_TITLE", "AzureAgentForge").strip() or "AzureAgentForge"
    sections, errors = collect()
    text = render(
        window_hours=window_h,
        now=datetime.now(timezone.utc),
        runs=sections.get("runs"),
        router=sections.get("router"),
        approvals=sections.get("approvals"),
        memory=sections.get("memory"),
        errors=errors,
        title=title,
    )
    # Always print: Log Analytics keeps the brief even with no chat target.
    print(text, flush=True)

    webhook = os.environ.get("BRIEF_WEBHOOK_URL", "").strip()
    if not webhook:
        print("morning_brief: no webhook configured (BRIEF_WEBHOOK_URL unset) — stdout only", flush=True)
        return 0
    try:
        status = post_webhook(webhook, build_payload(text, webhook))
    except Exception as exc:  # noqa: BLE001
        print(f"morning_brief: failed to post to webhook: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"morning_brief: posted brief to webhook (HTTP {status})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
