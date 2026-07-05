"""Tenant decommission executor.

Typed-confirmation teardown: the caller must pass ``confirm_text == "decommission
<slug>"`` verbatim or nothing runs. Steps are idempotent and tolerant — a
half-decommissioned tenant re-runs to completion:

1. cancel_open_issues — comment THEN cancel (No-Cancel-Without-Comment parity).
2. remove_agents — terminate then delete; a missing agent (404) is fine.
3. archive_company — PATCH status=archived; a 4xx (archive unsupported) is
   recorded ``pending``, never a failure.
4. expire_workspace — governor TTL sweep of the tenant partition.
5. verify_no_leakage — the partition must read empty afterward, else FAILED
   (the P2 acceptance probe, run headless here too).

The company row is archived, never hard-deleted (audit trail).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from tenantconsole.client import ApiClient, ApiError
from tenantconsole.contract import TenantContract
from tenantconsole.steps import FAILED, OK, PENDING, SKIPPED, Reporter, StepResult

__all__ = ["ConfirmationError", "run_decommission", "confirmation_phrase"]

CLOSED_STATUSES = ("done", "cancelled", "canceled")
DECOMMISSION_COMMENT = "Tenant decommissioned via tenant-console."


class ConfirmationError(ValueError):
    """The typed confirmation did not match ``decommission <slug>``."""


def confirmation_phrase(slug: str) -> str:
    return f"decommission {slug}"


def _resolve_company(client: ApiClient, display_name: str) -> Optional[dict]:
    for c in client.list_companies():
        if c.get("name") == display_name:
            return c
    return None


def _cancel_open_issues(client: ApiClient, company_id: str) -> StepResult:
    try:
        issues = client.list_company_issues(company_id)
    except ApiError as exc:
        return StepResult(FAILED, "cancel_open_issues", f"could not list issues: {exc}")
    cancelled: List[str] = []
    for issue in issues:
        if (issue.get("status") or "").lower() in CLOSED_STATUSES:
            continue
        iid = issue.get("id")
        try:
            client.create_issue_comment(iid, DECOMMISSION_COMMENT)  # comment BEFORE cancel
            client.patch_issue(iid, "cancelled")
        except ApiError as exc:
            return StepResult(
                FAILED, "cancel_open_issues", f"issue {issue.get('identifier') or iid}: {exc}"
            )
        cancelled.append(issue.get("identifier") or iid)
    return StepResult(
        OK, "cancel_open_issues", f"{len(cancelled)} open issue(s) cancelled",
        {"cancelled": cancelled},
    )


def _remove_agents(client: ApiClient, company_id: str) -> StepResult:
    try:
        agents = client.list_agents(company_id)
    except ApiError as exc:
        return StepResult(FAILED, "remove_agents", f"could not list agents: {exc}")
    removed: List[str] = []
    for agent in agents:
        aid = agent.get("id")
        for op in (client.terminate_agent, client.delete_agent):
            try:
                op(aid)
            except ApiError as exc:
                if exc.status != 404:
                    return StepResult(
                        FAILED, "remove_agents", f"agent {agent.get('name') or aid}: {exc}"
                    )
        removed.append(agent.get("name") or aid)
    return StepResult(OK, "remove_agents", f"{len(removed)} agent(s) removed", {"removed": removed})


def _archive_company(client: ApiClient, company_id: str) -> StepResult:
    try:
        company = client.patch_company(company_id, status="archived")
    except ApiError as exc:
        return StepResult(
            PENDING,
            "archive_company",
            f"archive not accepted (status {exc.status}) — company left as-is, "
            "archive manually or upgrade the API",
            {"company_id": company_id, "http_status": exc.status},
        )
    if isinstance(company, dict) and company.get("status") != "archived":
        return StepResult(
            PENDING,
            "archive_company",
            f"archive PATCH accepted but status is {company.get('status')!r} — verify manually",
            {"company_id": company_id, "status": company.get("status")},
        )
    return StepResult(OK, "archive_company", f"company {company_id} archived",
                      {"company_id": company_id})


def _expire_workspace(client: ApiClient, workspace: str) -> StepResult:
    try:
        counts = client.workspace_expire(workspace)
    except ApiError as exc:
        return StepResult(FAILED, "expire_workspace", f"workspace-expire failed: {exc}")
    return StepResult(
        OK,
        "expire_workspace",
        f"expired {counts.get('documents_expired')} doc(s), "
        f"{counts.get('session_rows_deleted')} session row(s)",
        {"counts": counts},
    )


def _verify_no_leakage(client: ApiClient, workspace: str) -> StepResult:
    try:
        remaining = client.list_memory(workspace, created_by=None, limit=200)
    except ApiError as exc:
        return StepResult(FAILED, "verify_no_leakage", f"leakage probe failed: {exc}")
    if remaining:
        return StepResult(
            FAILED,
            "verify_no_leakage",
            f"{len(remaining)} memory row(s) still reachable in {workspace} — "
            "re-run expire_workspace; tenant is NOT safe to release",
            {"remaining": len(remaining)},
        )
    return StepResult(OK, "verify_no_leakage", f"{workspace} reads empty", {"remaining": 0})


def _outcome(results: List[StepResult]) -> str:
    if any(r.status == FAILED for r in results):
        return "failed"
    if any(r.status == PENDING for r in results):
        return "decommissioned_pending"
    return "decommissioned"


def run_decommission(
    contract: TenantContract,
    client: ApiClient,
    confirm_text: str,
    *,
    report_path: Optional[str] = None,
    on_event: Optional[Callable[[str, str, Optional[StepResult]], None]] = None,
) -> Dict[str, Any]:
    """Tear a tenant down; return the JSON report dict.

    Raises :class:`ConfirmationError` unless ``confirm_text == "decommission
    <slug>"``. Halts on the first ``failed`` step.
    """
    from datetime import datetime, timezone

    expected = confirmation_phrase(contract.slug)
    if confirm_text != expected:
        raise ConfirmationError(
            f"confirmation must be exactly {expected!r} to decommission {contract.slug}"
        )

    reporter = Reporter(
        tenant=contract.slug,
        path=report_path,
        started_at=datetime.now(timezone.utc).isoformat(),
        kind="decommission",
    )

    def emit(name: str, phase: str, result: Optional[StepResult]) -> None:
        if on_event:
            on_event(name, phase, result)

    company = _resolve_company(client, contract.display_name)
    company_id = company.get("id") if company else None
    workspace = contract.workspace

    results: List[StepResult] = []

    def run(name: str, fn: Callable[[], StepResult]) -> bool:
        emit(name, "start", None)
        result = fn()
        results.append(result)
        emit(name, "end", result)
        return result.status != FAILED

    if company_id:
        if not run("cancel_open_issues", lambda: _cancel_open_issues(client, company_id)):
            return _finish(reporter, results, emit)
        if not run("remove_agents", lambda: _remove_agents(client, company_id)):
            return _finish(reporter, results, emit)
        if not run("archive_company", lambda: _archive_company(client, company_id)):
            return _finish(reporter, results, emit)
    else:
        skipped = StepResult(
            SKIPPED,
            "resolve_company",
            f"no company named {contract.display_name!r} — memory teardown only",
        )
        results.append(skipped)
        emit("resolve_company", "end", skipped)

    if not run("expire_workspace", lambda: _expire_workspace(client, workspace)):
        return _finish(reporter, results, emit)
    run("verify_no_leakage", lambda: _verify_no_leakage(client, workspace))
    return _finish(reporter, results, emit)


def _finish(
    reporter: Reporter,
    results: List[StepResult],
    emit: Callable[[str, str, Optional[StepResult]], None],
) -> Dict[str, Any]:
    outcome = _outcome(results)
    report = reporter.finalize(results, outcome)
    emit("report", "report", None)
    return report
