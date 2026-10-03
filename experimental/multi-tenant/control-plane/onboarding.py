# Reference design — part of the multi-tenant roadmap. Phase 5 of
# docs/notes/plans/2026-07-22-full-multi-tenant.md: tenant onboarding.

"""Tenant onboarding — validation, planning, and checkpointed execution.

provision_tenant.py is the imperative "do it" path (it POSTs to the live
control-plane). This module is the offline core that runs BEFORE any provider
call: validate a tenant contract, plan ordered provisioning steps, and execute
them through an injectable driver with durable checkpoints. A bad contract
fails with a clear list of reasons instead of a half-planned tenant; a provider
failure can be resumed or compensated without claiming that the live Azure
path is wired.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Protocol
from uuid import uuid4

from user_tokens import ROLES


@dataclass(frozen=True)
class OnboardingStep:
    action: str
    detail: dict


@dataclass(frozen=True)
class CompletedStep:
    """Checkpoint for a step whose side effect has been acknowledged."""

    index: int
    output: dict[str, Any]


@dataclass(frozen=True)
class OnboardingRun:
    """Persisted, restart-safe state for one onboarding attempt."""

    run_id: str
    contract_fingerprint: str
    steps: tuple[OnboardingStep, ...]
    completed: tuple[CompletedStep, ...] = ()
    compensated: tuple[int, ...] = ()
    status: str = "pending"
    current_index: int | None = None
    error: str | None = None
    compensation_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "contract_fingerprint": self.contract_fingerprint,
            "steps": [
                {"action": step.action, "detail": step.detail}
                for step in self.steps
            ],
            "completed": [
                {"index": item.index, "output": item.output}
                for item in self.completed
            ],
            "compensated": list(self.compensated),
            "status": self.status,
            "current_index": self.current_index,
            "error": self.error,
            "compensation_errors": list(self.compensation_errors),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OnboardingRun":
        try:
            return cls(
                run_id=str(value["run_id"]),
                contract_fingerprint=str(value["contract_fingerprint"]),
                steps=tuple(
                    OnboardingStep(str(item["action"]), dict(item["detail"]))
                    for item in value["steps"]
                ),
                completed=tuple(
                    CompletedStep(int(item["index"]), dict(item["output"]))
                    for item in value.get("completed", [])
                ),
                compensated=tuple(int(index) for index in value.get("compensated", [])),
                status=str(value.get("status", "pending")),
                current_index=(
                    int(value["current_index"])
                    if value.get("current_index") is not None
                    else None
                ),
                error=value.get("error"),
                compensation_errors=tuple(
                    str(item) for item in value.get("compensation_errors", [])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OnboardingStateError("onboarding state is invalid") from exc


class OnboardingStateError(RuntimeError):
    """Raised when a run cannot safely be resumed or rolled back."""


class OnboardingExecutionError(RuntimeError):
    """Stable failure for one provisioning step."""

    def __init__(self, run_id: str, action: str) -> None:
        self.run_id = run_id
        self.action = action
        super().__init__(f"onboarding step failed: {action}")


class OnboardingRollbackError(RuntimeError):
    """Raised after compensation attempted every completed step but some failed."""

    def __init__(self, run_id: str, actions: tuple[str, ...]) -> None:
        self.run_id = run_id
        self.actions = actions
        super().__init__("onboarding rollback incomplete")


class OnboardingStateStore(Protocol):
    def load(self, run_id: str) -> OnboardingRun | None:
        ...

    def save(self, run: OnboardingRun) -> None:
        ...


class MemoryOnboardingStateStore:
    """Small in-memory store for tests and local dry runs."""

    def __init__(self) -> None:
        self._runs: dict[str, OnboardingRun] = {}

    def load(self, run_id: str) -> OnboardingRun | None:
        return self._runs.get(run_id)

    def save(self, run: OnboardingRun) -> None:
        self._runs[run.run_id] = run


class JsonOnboardingStateStore:
    """Atomically persist runs in one local state file.

    This is a reference implementation for a durable state boundary. A live
    deployment should provide the same protocol with a transactional database
    table or durable job store rather than sharing a filesystem between pods.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def load(self, run_id: str) -> OnboardingRun | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text())
            value = data.get(run_id)
            return OnboardingRun.from_dict(value) if value is not None else None
        except (OSError, json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise OnboardingStateError("onboarding state is unreadable") from exc

    def save(self, run: OnboardingRun) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = json.loads(self._path.read_text()) if self._path.exists() else {}
            if not isinstance(existing, dict):
                raise OnboardingStateError("onboarding state is invalid")
            existing[run.run_id] = run.to_dict()
            payload = json.dumps(existing, sort_keys=True, separators=(",", ":"))
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self._path.name}.", dir=self._path.parent
            )
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self._path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OnboardingStateError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise OnboardingStateError("onboarding state could not be saved") from exc


class PostgresOnboardingStateStore:
    """Transactional state store for a control-plane operator connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def load(self, run_id: str) -> OnboardingRun | None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT state_json FROM tenant_onboarding_runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
            self._conn.commit()
            if row is None:
                return None
            payload = row[0]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                raise OnboardingStateError("onboarding state is invalid")
            return OnboardingRun.from_dict(payload)
        except OnboardingStateError:
            self._conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            self._conn.rollback()
            raise OnboardingStateError("onboarding state could not be loaded") from exc
        finally:
            cur.close()

    def save(self, run: OnboardingRun) -> None:
        payload = json.dumps(run.to_dict(), sort_keys=True, separators=(",", ":"))
        cur = self._conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO tenant_onboarding_runs (
                    run_id, contract_fingerprint, state_json, status
                ) VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    contract_fingerprint = EXCLUDED.contract_fingerprint,
                    state_json = EXCLUDED.state_json,
                    status = EXCLUDED.status,
                    updated_at = now()
                """,
                (run.run_id, run.contract_fingerprint, payload, run.status),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            self._conn.rollback()
            raise OnboardingStateError("onboarding state could not be saved") from exc
        finally:
            cur.close()


class OnboardingDriver(Protocol):
    def apply(
        self,
        step: OnboardingStep,
        *,
        idempotency_key: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def compensate(
        self,
        step: OnboardingStep,
        *,
        output: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        ...


def _contract_fingerprint(contract: dict) -> str:
    try:
        encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("tenant contract must be JSON serializable") from exc
    return sha256(encoded).hexdigest()


class OnboardingExecutor:
    """Checkpointed executor over the pure provisioning plan.

    A driver must make each ``apply`` operation idempotent using the supplied
    key. The executor persists before and after every step, so a restart can
    safely replay only the unacknowledged step and then continue.
    """

    def __init__(self, store: OnboardingStateStore, driver: OnboardingDriver) -> None:
        self._store = store
        self._driver = driver

    def start(self, contract: dict, *, run_id: str | None = None) -> OnboardingRun:
        steps = tuple(plan_provisioning(contract))
        fingerprint = _contract_fingerprint(contract)
        if run_id:
            existing = self._store.load(run_id)
            if existing is not None:
                if existing.contract_fingerprint != fingerprint:
                    raise OnboardingStateError("run belongs to a different contract")
                return self.resume(run_id)
        run = OnboardingRun(
            run_id=run_id or uuid4().hex,
            contract_fingerprint=fingerprint,
            steps=steps,
        )
        self._store.save(run)
        return self._execute(run)

    def resume(self, run_id: str) -> OnboardingRun:
        run = self._require(run_id)
        if run.status in ("complete", "rolled_back"):
            return run
        if run.status in ("rolling_back", "rollback_failed"):
            raise OnboardingStateError("onboarding run requires rollback completion")
        return self._execute(run)

    def _execute(self, run: OnboardingRun) -> OnboardingRun:
        completed_indexes = {item.index for item in run.completed}
        context = {"run_id": run.run_id}
        current = run
        for index, step in enumerate(run.steps):
            if index in completed_indexes:
                continue
            current = replace(
                current,
                status="running",
                current_index=index,
                error=None,
            )
            self._store.save(current)
            try:
                output = self._driver.apply(
                    step,
                    idempotency_key=f"{run.run_id}:{index}:{step.action}",
                    context=context,
                )
                if not isinstance(output, dict):
                    raise TypeError("driver output must be an object")
            except Exception as exc:  # noqa: BLE001
                failed = replace(
                    current,
                    status="failed",
                    error=f"step {step.action} failed",
                )
                self._store.save(failed)
                raise OnboardingExecutionError(run.run_id, step.action) from exc
            current = replace(
                current,
                completed=current.completed + (CompletedStep(index, output),),
                current_index=None,
            )
            self._store.save(current)
            completed_indexes.add(index)

        complete = replace(current, status="complete", current_index=None, error=None)
        self._store.save(complete)
        return complete

    def rollback(self, run_id: str) -> OnboardingRun:
        run = self._require(run_id)
        if run.status == "rolled_back":
            return run
        if run.status == "complete":
            raise OnboardingStateError("completed onboarding cannot be rolled back")

        current = replace(run, status="rolling_back", current_index=None, error=None)
        self._store.save(current)
        compensated = set(current.compensated)
        failed_actions: list[str] = []
        context = {"run_id": run.run_id}
        steps_by_index = {index: step for index, step in enumerate(run.steps)}
        outputs = {item.index: item.output for item in run.completed}
        for item in reversed(run.completed):
            if item.index in compensated:
                continue
            step = steps_by_index[item.index]
            try:
                self._driver.compensate(step, output=outputs[item.index], context=context)
            except Exception:  # noqa: BLE001
                failed_actions.append(step.action)
                current = replace(
                    current,
                    error=f"compensation failed: {step.action}",
                    compensation_errors=tuple(failed_actions),
                )
                self._store.save(current)
                continue
            compensated.add(item.index)
            current = replace(current, compensated=tuple(sorted(compensated)))
            self._store.save(current)

        if failed_actions:
            failed = replace(
                current,
                status="rollback_failed",
                compensation_errors=tuple(failed_actions),
            )
            self._store.save(failed)
            raise OnboardingRollbackError(run.run_id, tuple(failed_actions))

        rolled_back = replace(
            current,
            status="rolled_back",
            current_index=None,
            error=None,
            compensation_errors=(),
        )
        self._store.save(rolled_back)
        return rolled_back

    def _require(self, run_id: str) -> OnboardingRun:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        run = self._store.load(run_id)
        if run is None:
            raise OnboardingStateError("onboarding run not found")
        return run


_REQUIRED_STR = ("slug", "display_name", "primary_email", "vertical", "pack")


def validate_contract(contract: dict) -> list[str]:
    """Return a list of human-readable problems (empty == valid)."""
    errors: list[str] = []
    c = contract or {}

    for field in _REQUIRED_STR:
        val = c.get(field)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"{field} is required")

    email = c.get("primary_email")
    if isinstance(email, str) and email and "@" not in email:
        errors.append("primary_email must be an email address")

    cap = c.get("daily_budget_cap")
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
        errors.append("daily_budget_cap must be a positive number")

    users = c.get("users")
    if not isinstance(users, list) or not users:
        errors.append("at least one user is required")
    else:
        for i, u in enumerate(users):
            if not isinstance(u, dict):
                errors.append(f"users[{i}] must be an object")
                continue
            if "@" not in str(u.get("email", "")):
                errors.append(f"users[{i}].email must be an email address")
            if u.get("role") not in ROLES:
                errors.append(f"users[{i}].role must be one of {ROLES}")
        if not any(isinstance(u, dict) and u.get("role") == "owner" for u in users):
            errors.append("exactly one user must have the 'owner' role")

    return errors


def plan_provisioning(contract: dict) -> list[OnboardingStep]:
    """Validate then produce the ordered provisioning steps. Raises ValueError
    (with every reason) on an invalid contract — never a partial plan.

    Order matters: the tenant record and its budget/workspace exist before any
    user is attached, and the pack is enabled last (it may reference users)."""
    errors = validate_contract(contract)
    if errors:
        raise ValueError("invalid tenant contract: " + "; ".join(errors))

    steps: list[OnboardingStep] = [
        OnboardingStep("create_tenant", {
            "slug": contract["slug"],
            "display_name": contract["display_name"],
            "vertical": contract["vertical"],
        }),
        OnboardingStep("set_budget", {"daily_budget_cap": contract["daily_budget_cap"]}),
        OnboardingStep("provision_workspace", {"slug": contract["slug"]}),
    ]
    # Owner first, so the tenant always has an administrator before members land.
    for user in sorted(contract["users"], key=lambda u: ROLES.index(u["role"]), reverse=True):
        steps.append(OnboardingStep("create_user", {
            "email": user["email"], "role": user["role"],
        }))
    steps.append(OnboardingStep("enable_pack", {"pack": contract["pack"]}))
    return steps
