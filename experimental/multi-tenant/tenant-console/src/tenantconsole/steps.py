"""The headless tenant provisioning executor.

``run_provision`` walks an ordered, idempotent step list — preflight, company,
agents, instructions, seed memories, channels, smoke — reporting each step
through an ``on_event`` callback (the CLI prints it; the P2 console will SSE it).
Every step converges on re-run: it finds-or-creates, then patches only the keys
the executor owns, so a second run over a live tenant creates nothing.

Design contracts held here:

- The tenant binding lives in each agent's ``adapterConfig.env`` (five keys:
  GOVERNOR_WORKSPACE / HONCHO_APP_ID / ROUTER_TENANT_ID /
  ROUTER_TENANT_DAILY_BUDGET_USD / ROUTER_BUDGET_ENVELOPE_USD). Agent NAME is
  the slug; ``title`` carries the human label.
- Instructions round-trip is verified byte-exact — a mismatch fails the step.
- Seed memories dedupe on the governor's ``snippet`` (content[:160]).
- Channel binding never fails: it records intended bindings as ``pending`` and
  the PaperClip-issues intake surface as ``ok``.
- Smoke retries around non-deterministic agent wake: poll,
  nudge up to ``max_retries``, honour the overall timeout, assert the fixture's
  ``expect`` block mechanically.

The step methods are pure orchestration over :class:`~tenantconsole.client.ApiClient`;
they never sleep or read the clock directly (both are injected) so tests run
instantly and offline.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from tenantconsole.client import ApiClient, ApiError
from tenantconsole.contract import TenantContract
from tenantconsole.playbook import (
    Pack,
    load_seed_memories,
    load_smoke_fixture,
    render_agent,
    validate_pack,
)

__all__ = [
    "StepResult",
    "Provisioner",
    "Reporter",
    "run_provision",
    "build_plan",
    "agent_env",
    "agent_slug",
    "usd_str",
]

# Step outcome vocabulary.
OK = "ok"
SKIPPED = "skipped"
PENDING = "pending"
FAILED = "failed"

ADAPTER_TYPE = "hermes_local"
AGENT_TIMEOUT_SEC = 300
ENTRY_FILE = "AGENTS.md"
PAPERCLIP_ISSUES_KINDS = ("paperclip-issues", "paperclip_issues")

# adapterConfig keys the executor owns (drift comparison touches only these, so
# platform-added bundle keys like mode/rootPath/entryFile survive a converge).
OWNED_ADAPTER_KEYS = ("model", "timeoutSec", "persistSession", "enabledToolsets")


@dataclass
class StepResult:
    """One provisioning step's outcome."""

    status: str  # OK | SKIPPED | PENDING | FAILED
    name: str
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (OK, SKIPPED, PENDING)


# --------------------------------------------------------------------------
# Derivations
# --------------------------------------------------------------------------

def usd_str(value: float) -> str:
    """Render a USD budget as a plain string env value (no trailing artifacts)."""
    return str(round(float(value), 2))


def agent_slug(slug_prefix: str, tenant_slug: str) -> str:
    """The agent NAME/slug: ``<slug_prefix>-<tenant_slug>`` (e.g. intake-example-fieldservice)."""
    return f"{slug_prefix}-{tenant_slug}"


def agent_env(contract: TenantContract) -> Dict[str, str]:
    """The five-key tenant binding written to every agent's ``adapterConfig.env``."""
    return {
        "GOVERNOR_WORKSPACE": contract.workspace,
        "HONCHO_APP_ID": contract.workspace,
        "ROUTER_TENANT_ID": contract.slug,
        "ROUTER_TENANT_DAILY_BUDGET_USD": usd_str(contract.daily_usd),
        "ROUTER_BUDGET_ENVELOPE_USD": usd_str(contract.per_run_usd),
    }


def _desired_adapter_config(spec, contract: TenantContract) -> Dict[str, Any]:
    return {
        "model": spec.model,
        "timeoutSec": AGENT_TIMEOUT_SEC,
        "persistSession": True,
        "enabledToolsets": list(spec.toolsets),
        "env": agent_env(contract),
    }


def _ordered_specs(pack: Pack) -> List[Any]:
    """Roster order for provisioning: roots (reports_to=None) before their reports.

    A parent's PaperClip id must exist before a child sets ``reportsTo``. For the
    example pack this yields coordinator, then intake.
    """
    remaining = list(pack.agents)
    placed: List[Any] = []
    placed_roles = set()
    # stable topological-ish pass; cycles or dangling refs fall through last
    while remaining:
        progressed = False
        for spec in list(remaining):
            if spec.reports_to is None or spec.reports_to in placed_roles:
                placed.append(spec)
                placed_roles.add(spec.role)
                remaining.remove(spec)
                progressed = True
        if not progressed:
            placed.extend(remaining)  # dangling reports_to — provision anyway
            break
    return placed


# --------------------------------------------------------------------------
# Reporter
# --------------------------------------------------------------------------

@dataclass
class Reporter:
    """Accumulates step results and writes the final JSON report."""

    tenant: str
    path: Optional[str] = None
    started_at: str = ""
    kind: str = "provision"

    def finalize(self, results: List[StepResult], outcome: str) -> Dict[str, Any]:
        import json
        from datetime import datetime, timezone

        report = {
            "tenant": self.tenant,
            "kind": self.kind,
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
            "steps": [
                {"name": r.name, "status": r.status, "detail": r.detail, "data": r.data}
                for r in results
            ],
        }
        if self.path:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, sort_keys=True)
                fh.write("\n")
        return report


# --------------------------------------------------------------------------
# Provisioner
# --------------------------------------------------------------------------

class Provisioner:
    """Ordered, idempotent provisioning steps over one tenant contract."""

    def __init__(
        self,
        contract: TenantContract,
        pack: Pack,
        client: ApiClient,
        *,
        skip_smoke: bool = False,
        poll_interval_seconds: int = 15,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        self.contract = contract
        self.pack = pack
        self.client = client
        self.skip_smoke = skip_smoke
        self.poll_interval = poll_interval_seconds
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        # cross-step state
        self.company: Optional[dict] = None
        self.agents_by_role: Dict[str, dict] = {}

    def ordered_steps(self) -> List[tuple]:
        return [
            ("preflight", self.preflight),
            ("ensure_company", self.ensure_company),
            ("ensure_agents", self.ensure_agents),
            ("upload_instructions", self.upload_instructions),
            ("seed_memories", self.seed_memories),
            ("bind_channels", self.bind_channels),
            ("smoke", self.smoke),
        ]

    # -- steps -------------------------------------------------------------

    def preflight(self) -> StepResult:
        try:
            self.client.whoami()
            self.client.list_companies()
            self.client.list_memory(self.contract.workspace, created_by=None, limit=1)
        except ApiError as exc:
            return StepResult(FAILED, "preflight", f"stack unreachable: {exc}")
        problems = validate_pack(self.pack)
        if problems:
            return StepResult(
                FAILED,
                "preflight",
                f"playbook pack '{self.pack.vertical}' invalid: {'; '.join(problems)}",
            )
        return StepResult(
            OK,
            "preflight",
            f"stack reachable, pack '{self.pack.vertical}' valid",
            {"vertical": self.pack.vertical, "workspace": self.contract.workspace},
        )

    def ensure_company(self) -> StepResult:
        target_budget = self.contract.monthly_budget_cents()
        try:
            companies = self.client.list_companies()
            existing = next(
                (c for c in companies if c.get("name") == self.contract.display_name), None
            )
            if existing is None:
                self.company = self.client.create_company(
                    self.contract.display_name,
                    description=f"Tenant {self.contract.slug} ({self.contract.vertical}) — tenant-console",
                    budget_monthly_cents=target_budget,
                )
                return StepResult(
                    OK,
                    "ensure_company",
                    f"created company {self.company.get('id')}",
                    {"company_id": self.company.get("id"), "created": True},
                )
            self.company = existing
            cid = existing.get("id")
            if existing.get("budgetMonthlyCents") != target_budget:
                self.company = self.client.patch_company(cid, budgetMonthlyCents=target_budget)
                return StepResult(
                    OK,
                    "ensure_company",
                    f"converged budget → {target_budget} cents on {cid}",
                    {"company_id": cid, "created": False, "converged": True},
                )
            return StepResult(
                OK,
                "ensure_company",
                f"company {cid} already current",
                {"company_id": cid, "created": False, "converged": False},
            )
        except ApiError as exc:
            return StepResult(FAILED, "ensure_company", f"company step failed: {exc}")

    def ensure_agents(self) -> StepResult:
        cid = (self.company or {}).get("id")
        if not cid:
            return StepResult(FAILED, "ensure_agents", "no company id from ensure_company")
        try:
            existing = {a.get("name"): a for a in self.client.list_agents(cid)}
        except ApiError as exc:
            return StepResult(FAILED, "ensure_agents", f"could not list agents: {exc}")

        created = converged = current = 0
        details: Dict[str, Any] = {}
        for spec in _ordered_specs(self.pack):
            name = agent_slug(spec.slug_prefix, self.contract.slug)
            title = spec.name_template
            variables = self.contract.render_variables()
            if title:
                from tenantconsole.playbook import render_template

                title = render_template(title, variables)
            reports_to_id = None
            if spec.reports_to is not None:
                parent = self.agents_by_role.get(spec.reports_to)
                reports_to_id = parent.get("id") if parent else None
            desired_cfg = _desired_adapter_config(spec, self.contract)
            target_budget = self.contract.monthly_budget_cents()

            try:
                if name not in existing:
                    payload = {
                        "name": name,
                        "title": title,
                        "role": spec.role,
                        "adapterType": ADAPTER_TYPE,
                        "adapterConfig": desired_cfg,
                        "budgetMonthlyCents": target_budget,
                    }
                    if reports_to_id:
                        payload["reportsTo"] = reports_to_id
                    agent = self.client.create_agent(cid, payload)
                    created += 1
                    details[spec.role] = {"id": agent.get("id"), "action": "created"}
                else:
                    agent = existing[name]
                    patch = self._agent_drift_patch(
                        agent, desired_cfg, target_budget, reports_to_id
                    )
                    if patch:
                        agent = self.client.patch_agent(agent.get("id"), patch)
                        converged += 1
                        details[spec.role] = {
                            "id": agent.get("id"),
                            "action": "converged",
                            "keys": sorted(patch.keys()),
                        }
                    else:
                        current += 1
                        details[spec.role] = {"id": agent.get("id"), "action": "current"}
                self.agents_by_role[spec.role] = agent
            except ApiError as exc:
                return StepResult(
                    FAILED, "ensure_agents", f"agent '{name}' step failed: {exc}"
                )

        return StepResult(
            OK,
            "ensure_agents",
            f"{created} created, {converged} converged, {current} current",
            {"created": created, "converged": converged, "current": current, "agents": details},
        )

    @staticmethod
    def _agent_drift_patch(
        agent: dict, desired_cfg: dict, target_budget: int, reports_to_id: Optional[str]
    ) -> Dict[str, Any]:
        """Return the minimal PATCH to converge an existing agent, or ``{}``.

        Compares only executor-owned keys. adapterConfig is patched partially
        (no ``replaceAdapterConfig``) so platform-added bundle keys survive.
        """
        patch: Dict[str, Any] = {}
        cfg = agent.get("adapterConfig") or {}
        cfg_patch: Dict[str, Any] = {}
        for key in OWNED_ADAPTER_KEYS:
            if cfg.get(key) != desired_cfg[key]:
                cfg_patch[key] = desired_cfg[key]
        existing_env = cfg.get("env") or {}
        desired_env = desired_cfg["env"]
        if any(existing_env.get(k) != v for k, v in desired_env.items()):
            cfg_patch["env"] = desired_env
        if cfg_patch:
            patch["adapterConfig"] = cfg_patch
        if agent.get("budgetMonthlyCents") != target_budget:
            patch["budgetMonthlyCents"] = target_budget
        if reports_to_id and agent.get("reportsTo") != reports_to_id:
            patch["reportsTo"] = reports_to_id
        return patch

    def upload_instructions(self) -> StepResult:
        variables = self.contract.render_variables()
        details: Dict[str, Any] = {}
        for role, agent in self.agents_by_role.items():
            agent_id = agent.get("id")
            try:
                content = render_agent(self.pack, role, variables)
            except (KeyError, ValueError) as exc:
                return StepResult(
                    FAILED, "upload_instructions", f"render '{role}' failed: {exc}"
                )
            try:
                self.client.put_instructions_file(agent_id, ENTRY_FILE, content)
                self.client.set_managed_bundle(agent_id, ENTRY_FILE)
                roundtrip = self.client.get_instructions_file(agent_id, ENTRY_FILE)
            except ApiError as exc:
                return StepResult(
                    FAILED, "upload_instructions", f"agent '{role}' bundle failed: {exc}"
                )
            got = (roundtrip or {}).get("content")
            if got != content:
                return StepResult(
                    FAILED,
                    "upload_instructions",
                    f"agent '{role}' AGENTS.md round-trip mismatch — "
                    "re-run after checking the instructions bundle mode",
                    {"role": role, "expected_len": len(content), "got_len": len(got or "")},
                )
            details[role] = {"id": agent_id, "bytes": len(content)}
        return StepResult(
            OK, "upload_instructions", f"{len(details)} AGENTS.md verified", {"agents": details}
        )

    def seed_memories(self) -> StepResult:
        variables = self.contract.render_variables()
        try:
            seeds = load_seed_memories(self.pack, variables)
        except ValueError as exc:
            return StepResult(FAILED, "seed_memories", f"seed file invalid: {exc}")
        ws = self.contract.workspace
        try:
            existing = self.client.list_memory(ws, created_by="operator", limit=200)
        except ApiError as exc:
            return StepResult(FAILED, "seed_memories", f"could not list memory: {exc}")
        existing_snippets = {m.get("snippet") for m in existing}

        admitted, skipped = 0, 0
        admitted_keys, skipped_keys = [], []
        for seed in seeds:
            content = seed["content"]
            if content[:160] in existing_snippets:
                skipped += 1
                skipped_keys.append(seed["seed_key"])
                continue
            payload = {
                "content": content,
                "workspace_name": ws,
                "observer": "operator",
                # A5: seeds are ABOUT the tenant's principal user — resolve the
                # canonical user peer (same deploy-time input as the governor
                # default and the pc-* helpers) instead of a hardcoded literal,
                # so seeded facts land on the peer readers actually query.
                "observed": (os.environ.get("HONCHO_USER_PEER_ID") or "").strip() or "user",
                "created_by_peer": "operator",
                "memory_class": seed["memory_class"],
                "scope_kind": "workspace",
                "source_type": "operator_entered",
                "verification_state": "confirmed",
                "pin_request": bool(seed["pin_request"]),
                "context": f"tenant-console seed {seed['seed_key']}",
            }
            try:
                self.client.admit_memory(payload)
            except ApiError as exc:
                return StepResult(
                    FAILED, "seed_memories", f"admit '{seed['seed_key']}' failed: {exc}"
                )
            admitted += 1
            admitted_keys.append(seed["seed_key"])
        return StepResult(
            OK,
            "seed_memories",
            f"{admitted} admitted, {skipped} already present",
            {
                "admitted": admitted,
                "skipped": skipped,
                "admitted_keys": admitted_keys,
                "skipped_keys": skipped_keys,
            },
        )

    def bind_channels(self) -> StepResult:
        """Record channel bindings. Never fails (P1 records intent; P2/P3 bind)."""
        intake = self.agents_by_role.get("intake") or {}
        channels: List[Dict[str, Any]] = [
            {
                "kind": "paperclip-issues",
                "status": OK,
                "binding": {
                    "companyId": (self.company or {}).get("id"),
                    "assigneeAgentId": intake.get("id"),
                },
            }
        ]
        for ch in self.contract.channels:
            if ch.kind in PAPERCLIP_ISSUES_KINDS:
                channels.append({"kind": ch.kind, "status": OK, "binding": dict(ch.config)})
            else:
                channels.append(
                    {
                        "kind": ch.kind,
                        "status": PENDING,
                        "binding": {
                            "companyId": (self.company or {}).get("id"),
                            "tenantId": self.contract.slug,
                            **ch.config,
                        },
                    }
                )
        pending = [c["kind"] for c in channels if c["status"] == PENDING]
        status = PENDING if pending else OK
        detail = (
            f"{len(channels)} channel(s); pending: {', '.join(pending)}"
            if pending
            else f"{len(channels)} channel(s) bound"
        )
        return StepResult(status, "bind_channels", detail, {"channels": channels})

    def smoke(self) -> StepResult:
        if self.skip_smoke:
            return StepResult(SKIPPED, "smoke", "--skip-smoke")
        cid = (self.company or {}).get("id")
        intake = self.agents_by_role.get("intake")
        if not intake:
            return StepResult(FAILED, "smoke", "no intake agent to smoke")
        variables = self.contract.render_variables()
        try:
            fixture = load_smoke_fixture(self.pack, variables)
        except ValueError as exc:
            return StepResult(FAILED, "smoke", f"smoke fixture invalid: {exc}")

        try:
            issue = self.client.create_issue(
                cid, fixture["title"], fixture["prompt"], intake.get("id"), status="todo"
            )
        except ApiError as exc:
            return StepResult(FAILED, "smoke", f"could not open smoke issue: {exc}")
        issue_id = issue.get("id")
        identifier = issue.get("identifier") or issue_id

        our_comment_ids: set = set()
        timeout = int(fixture["timeout_seconds"])
        retry_after = int(fixture["retry_on_no_wake_seconds"])
        max_retries = int(fixture["max_retries"])

        start = self._clock()
        deadline = start + timeout
        next_nudge_at = start + retry_after
        retries_used = 0
        agent_comments: List[dict] = []

        while True:
            try:
                comments = self.client.list_issue_comments(issue_id)
            except ApiError as exc:
                return StepResult(FAILED, "smoke", f"could not read comments: {exc}")
            agent_comments = [c for c in comments if c.get("id") not in our_comment_ids]
            if agent_comments:
                break
            now = self._clock()
            if now >= deadline:
                return StepResult(
                    FAILED,
                    "smoke",
                    f"intake agent never woke within {timeout}s "
                    f"({retries_used} nudge(s)) — assume flaky wake (non-deterministic), re-run",
                    {"issue": identifier, "elapsed": round(now - start, 1), "nudges": retries_used},
                )
            if now >= next_nudge_at and retries_used < max_retries:
                try:
                    nudge = self.client.create_issue_comment(
                        issue_id,
                        f"Tenant-console smoke check for {self.contract.slug}: still waiting on "
                        "the intake agent — nudging.",
                    )
                    our_comment_ids.add(nudge.get("id"))
                except ApiError as exc:
                    return StepResult(FAILED, "smoke", f"could not post nudge: {exc}")
                retries_used += 1
                next_nudge_at = now + retry_after
            self._sleep(self.poll_interval)

        final_issue = self.client.get_issue(issue_id) or issue
        status_now = final_issue.get("status")
        elapsed = round(self._clock() - start, 1)

        verdict = _assert_expect(fixture["expect"], agent_comments, status_now)
        transcript_tail = [
            (c.get("body") or "")[:280] for c in agent_comments[-3:]
        ]
        data = {
            "issue": identifier,
            "elapsed": elapsed,
            "nudges": retries_used,
            "agent_comments": len(agent_comments),
            "issue_status": status_now,
            "transcript_tail": transcript_tail,
        }
        if verdict is None:
            return StepResult(OK, "smoke", f"smoke passed for {identifier} in {elapsed}s", data)
        return StepResult(FAILED, "smoke", f"smoke assertion failed: {verdict}", data)


def _assert_expect(
    expect: Dict[str, Any], agent_comments: List[dict], issue_status: Optional[str]
) -> Optional[str]:
    """Return ``None`` when every expectation holds, else the first failure string."""
    bodies = [(c.get("body") or "") for c in agent_comments]
    concatenated = "\n".join(bodies)
    lowered = concatenated.lower()

    must_any = expect.get("must_contain_any") or []
    if must_any and not any(str(s).lower() in lowered for s in must_any):
        return f"none of must_contain_any present: {must_any}"

    for forbidden in expect.get("must_not_contain") or []:
        if str(forbidden).lower() in lowered:
            return f"forbidden phrase present: {forbidden!r}"

    min_chars = expect.get("comment_min_chars")
    if isinstance(min_chars, int):
        longest = max((len(b) for b in bodies), default=0)
        if longest < min_chars:
            return f"longest agent comment {longest} chars < required {min_chars}"

    status_in = expect.get("status_in") or []
    if status_in and issue_status not in status_in:
        return f"issue status {issue_status!r} not in {status_in}"
    return None


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def _outcome(results: List[StepResult]) -> str:
    if any(r.status == FAILED for r in results):
        return "failed"
    if any(r.status == PENDING for r in results):
        return "provisioned_pending"
    return "provisioned"


def run_provision(
    contract: TenantContract,
    pack: Pack,
    client: ApiClient,
    *,
    report_path: Optional[str] = None,
    skip_smoke: bool = False,
    poll_interval_seconds: int = 15,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    on_event: Optional[Callable[[str, str, Optional[StepResult]], None]] = None,
) -> Dict[str, Any]:
    """Provision a tenant end-to-end; return the JSON report dict.

    Halts on the first ``failed`` step (every step is idempotent, so a re-run
    resumes cleanly). ``on_event(name, phase, result)`` fires with
    ``phase in {"start","end","report"}``; ``result`` is ``None`` on start.
    """
    from datetime import datetime, timezone

    prov = Provisioner(
        contract,
        pack,
        client,
        skip_smoke=skip_smoke,
        poll_interval_seconds=poll_interval_seconds,
        clock=clock,
        sleep=sleep,
    )
    reporter = Reporter(
        tenant=contract.slug,
        path=report_path,
        started_at=datetime.now(timezone.utc).isoformat(),
        kind="provision",
    )

    def emit(name: str, phase: str, result: Optional[StepResult]) -> None:
        if on_event:
            on_event(name, phase, result)

    results: List[StepResult] = []
    for name, fn in prov.ordered_steps():
        emit(name, "start", None)
        result = fn()
        results.append(result)
        emit(name, "end", result)
        if result.status == FAILED:
            break

    outcome = _outcome(results)
    report = reporter.finalize(results, outcome)
    emit("report", "report", None)
    return report
