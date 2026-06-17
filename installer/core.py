"""Forge Console core — the testable, framework-free half of the installer.

Everything here is plain Python: prerequisite detection, tfvars rendering,
backend handling, and a one-at-a-time subprocess runner that streams output
line-by-line. ``app.py`` wraps this in FastAPI routes; the tests exercise
this module directly with stub commands.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
TF_DIR = REPO_ROOT / "infrastructure" / "environments" / "dev"
PROFILES_DIR = REPO_ROOT / "infrastructure" / "profiles"

VALID_PROFILES = ("cost-optimized", "hardened")

# Azure region slugs are lowercase alphanumerics ("eastus", "westeurope").
_LOCATION_RE = re.compile(r"^[a-z0-9]{3,30}$")
_ENVIRONMENT_RE = re.compile(r"^[a-z][a-z0-9]{1,11}$")
_SUBSCRIPTION_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------

def _version_of(cmd: list[str]) -> Optional[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    first = (out.stdout or out.stderr).strip().splitlines()
    return first[0][:80] if first else "found"


def run_checks() -> dict:
    """Detect the tools each deployment path needs. Pure detection, no writes."""
    checks: dict[str, dict] = {}

    tf = shutil.which("terraform")
    checks["terraform"] = {
        "found": bool(tf),
        "detail": _version_of(["terraform", "version"]) if tf else "not on PATH",
        "required_for": "azure",
    }

    az = shutil.which("az")
    checks["az"] = {
        "found": bool(az),
        "detail": _version_of(["az", "version", "--query", "\"azure-cli\"", "-o", "tsv"]) if az else "not on PATH",
        "required_for": "azure",
    }

    docker = shutil.which("docker")
    docker_detail = "not on PATH"
    docker_ok = False
    if docker:
        docker_detail = _version_of(["docker", "--version"]) or "found (daemon state unknown)"
        ping = None
        try:
            ping = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                                  capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            pass
        docker_ok = bool(ping and ping.returncode == 0)
        if docker and not docker_ok:
            docker_detail += " — daemon not responding (start Docker Desktop for the local path)"
    checks["docker"] = {"found": docker_ok, "detail": docker_detail, "required_for": "local"}

    checks["azure_login"] = azure_account_check() if az else {
        "found": False, "detail": "az CLI missing", "required_for": "azure",
    }
    return checks


def azure_account_check() -> dict:
    try:
        out = subprocess.run(["az", "account", "show", "-o", "json"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {"found": False, "detail": "az not runnable", "required_for": "azure"}
    if out.returncode != 0:
        return {"found": False, "detail": "not logged in — run: az login", "required_for": "azure"}
    try:
        acct = json.loads(out.stdout)
        return {
            "found": True,
            "detail": f"{acct.get('name', '?')} ({acct.get('id', '?')})",
            "subscription_id": acct.get("id"),
            "required_for": "azure",
        }
    except json.JSONDecodeError:
        return {"found": False, "detail": "unexpected az output", "required_for": "azure"}


def list_subscriptions() -> list[dict]:
    try:
        out = subprocess.run(["az", "account", "list", "-o", "json"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return []
        return [
            {"id": s.get("id"), "name": s.get("name"), "isDefault": s.get("isDefault", False)}
            for s in json.loads(out.stdout)
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Configuration rendering
# ---------------------------------------------------------------------------

@dataclass
class DeployConfig:
    subscription_id: str
    location: str = "eastus"
    environment: str = "dev"
    profile: str = "cost-optimized"
    telegram_enabled: bool = False
    discord_enabled: bool = False
    ai_foundry_endpoint: str = ""
    ai_foundry_deployment_id: str = ""
    owner_email: str = ""
    # Azure AD object IDs granted Key Vault Secrets Officer, so the operator (and
    # any CI principal) can seed secrets into the RBAC-authorized vault. Without
    # at least the deploying user's OID, the post-create secret seeding 403s.
    keyvault_admin_object_ids: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        errs = []
        if not _SUBSCRIPTION_RE.match(self.subscription_id or ""):
            errs.append("subscription_id must be a GUID")
        if not _LOCATION_RE.match(self.location or ""):
            errs.append("location must be an Azure region slug like 'eastus'")
        if not _ENVIRONMENT_RE.match(self.environment or ""):
            errs.append("environment must be 2-12 chars, lowercase alphanumeric, letter first")
        if self.profile not in VALID_PROFILES:
            errs.append(f"profile must be one of {VALID_PROFILES}")
        if self.ai_foundry_endpoint and not self.ai_foundry_endpoint.startswith("https://"):
            errs.append("ai_foundry_endpoint must be an https:// URL")
        if self.owner_email and ("@" not in self.owner_email or '"' in self.owner_email):
            errs.append("owner_email doesn't look like an email address")
        for oid in self.keyvault_admin_object_ids:
            if not _SUBSCRIPTION_RE.match(oid or ""):
                errs.append(f"keyvault_admin_object_ids entry {oid!r} must be a GUID")
                break
        return errs


def _hcl_str(value: str) -> str:
    """Quote a string for HCL. Inputs are pre-validated; this is belt-and-braces."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_tfvars(cfg: DeployConfig) -> str:
    """Render terraform.tfvars content, `terraform fmt`-canonical (aligned =).
    Profile values are merged at plan time via -var-file, so this only
    carries the user's own answers."""
    pairs: list[tuple[str, str]] = [
        ("subscription_id", _hcl_str(cfg.subscription_id)),
        ("location", _hcl_str(cfg.location)),
        ("environment", _hcl_str(cfg.environment)),
        ("telegram_enabled", str(cfg.telegram_enabled).lower()),
        ("discord_enabled", str(cfg.discord_enabled).lower()),
    ]
    if cfg.ai_foundry_endpoint:
        pairs.append(("ai_foundry_endpoint", _hcl_str(cfg.ai_foundry_endpoint)))
    if cfg.ai_foundry_deployment_id:
        pairs.append(("ai_foundry_deployment_id", _hcl_str(cfg.ai_foundry_deployment_id)))
    if cfg.owner_email:
        pairs.append(("owner_email", _hcl_str(cfg.owner_email)))
    if cfg.keyvault_admin_object_ids:
        ids = ", ".join(_hcl_str(o) for o in cfg.keyvault_admin_object_ids)
        pairs.append(("keyvault_admin_object_ids", f"[{ids}]"))
    width = max(len(k) for k, _ in pairs)
    body = "\n".join(f"{k.ljust(width)} = {v}" for k, v in pairs)
    header = ("# Generated by the Forge Console installer. NEVER commit this file\n"
              "# (the repo .gitignore already excludes *.tfvars).\n")
    return header + body + "\n"


BACKEND_OVERRIDE = """\
# Generated by the Forge Console installer.
# Overrides the azurerm backend in backend.tf with LOCAL state so a first
# deploy needs zero pre-provisioned state storage. Terraform loads
# *_override.tf files last, so this wins without editing backend.tf.
# For team use, migrate to the azurerm backend later:
#   1. fill in backend.tf with a real storage account
#   2. delete this file
#   3. terraform init -migrate-state
terraform {
  backend "local" {}
}
"""


def write_config(cfg: DeployConfig, tf_dir: Path = TF_DIR) -> dict:
    """Write terraform.tfvars + backend_override.tf. Returns what was written."""
    errs = cfg.validate()
    if errs:
        raise ValueError("; ".join(errs))
    tfvars_path = tf_dir / "terraform.tfvars"
    backend_path = tf_dir / "backend_override.tf"
    tfvars_path.write_text(render_tfvars(cfg))
    backend_path.write_text(BACKEND_OVERRIDE)
    return {
        "tfvars": str(tfvars_path),
        "backend_override": str(backend_path),
        "profile_file": str(PROFILES_DIR / f"{cfg.profile}.tfvars"),
    }


# ---------------------------------------------------------------------------
# Step runner — one subprocess at a time, streamed
# ---------------------------------------------------------------------------

def build_step_command(step: str, profile: str, tf_dir: Path = TF_DIR,
                       repo_root: Path = REPO_ROOT) -> list[str]:
    """Map a step name to its exact command. Steps are a fixed allowlist —
    the web layer never passes raw commands."""
    profile_file = str((PROFILES_DIR / f"{profile}.tfvars").resolve())
    tf = ["terraform", f"-chdir={tf_dir}"]
    commands = {
        "init":     tf + ["init", "-input=false"],
        "validate": tf + ["validate"],
        "plan":     tf + ["plan", "-input=false", f"-var-file={profile_file}", f"-out={PLAN_FILE}"],
        "apply":    tf + ["apply", "-input=false", PLAN_FILE],
        "destroy":  tf + ["destroy", "-input=false", f"-var-file={profile_file}", "-auto-approve"],
        "output":   tf + ["output", "-no-color"],
        "compose-up":   ["docker", "compose", "up", "-d", "--build"],
        "compose-down": ["docker", "compose", "down"],
        "compose-ps":   ["docker", "compose", "ps"],
    }
    if step not in commands:
        raise ValueError(f"unknown step: {step}")
    return commands[step]


# Steps that change real infrastructure or remove resources. The API layer
# requires a typed confirmation (the environment name) before running these.
DANGEROUS_STEPS = {"apply", "destroy", "compose-down"}

# plan/apply/destroy must not run before the config exists.
NEEDS_CONFIG = {"plan", "apply", "destroy"}

# The saved plan file produced by `plan` and consumed verbatim by `apply`.
PLAN_FILE = "tfplan"

# A separate, deliberately-distinct token the operator must type to approve an
# apply whose plan would delete or replace resources. Louder than the ordinary
# environment-name confirmation so a destructive apply can't be waved through.
DESTROY_APPROVAL_TOKEN = "approve-destroy"


# ---------------------------------------------------------------------------
# Destroy-aware apply gate
# ---------------------------------------------------------------------------

def plan_has_destroy(plan_json: dict) -> tuple[bool, list[str]]:
    """Return (has_destroy, [addresses]) for a `terraform show -json` plan.

    A destroy is any resource change whose ``actions`` contain ``"delete"``:
    a pure delete (``["delete"]``) and both replace orderings
    (``["delete","create"]`` / ``["create","delete"]``) all count. Pure-Python,
    no Terraform invocation — so it is trivially unit-testable.
    """
    destroyed: list[str] = []
    for rc in (plan_json or {}).get("resource_changes", []) or []:
        actions = ((rc or {}).get("change") or {}).get("actions") or []
        if "delete" in actions:
            addr = rc.get("address") or rc.get("name") or "<unknown>"
            destroyed.append(addr)
    return (bool(destroyed), destroyed)


def show_plan_json(profile: str, tf_dir: Path = TF_DIR) -> dict:
    """Run ``terraform show -json <planfile>`` and return the parsed JSON.

    Reads the saved plan only; never re-plans and never applies. Raises
    ``RuntimeError`` if the plan file is missing or terraform/JSON parsing
    fails, so callers can surface a clear error instead of guessing.
    """
    plan_path = tf_dir / PLAN_FILE
    if not plan_path.exists():
        raise RuntimeError("no saved plan — run 'plan' before inspecting or applying")
    cmd = ["terraform", f"-chdir={tf_dir}", "show", "-json", PLAN_FILE]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                             env={**os.environ, "TF_IN_AUTOMATION": "1"})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"could not read the saved plan: {e}") from e
    if out.returncode != 0:
        raise RuntimeError(f"terraform show failed: {(out.stderr or out.stdout).strip()[:200]}")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"could not parse plan JSON: {e}") from e


def inspect_saved_plan(profile: str, tf_dir: Path = TF_DIR) -> dict:
    """Summarise the saved plan for the apply gate: destroy verdict + addresses."""
    has_destroy, destroyed = plan_has_destroy(show_plan_json(profile, tf_dir))
    return {"has_destroy": has_destroy, "destroyed": destroyed}


@dataclass
class StepRun:
    step: str
    status: str = "running"          # running | succeeded | failed
    returncode: Optional[int] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    lines: list[str] = field(default_factory=list)


class Runner:
    """Runs one step at a time; output is buffered for SSE streaming."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: Optional[StepRun] = None
        self.history: list[StepRun] = []
        self._subscribers: list[queue.Queue] = []

    def busy(self) -> bool:
        return self.current is not None and self.current.status == "running"

    def start(self, step: str, cmd: list[str], cwd: Path = REPO_ROOT) -> StepRun:
        with self._lock:
            if self.busy():
                raise RuntimeError(f"a step is already running: {self.current.step}")
            run = StepRun(step=step)
            self.current = run
        thread = threading.Thread(target=self._execute, args=(run, cmd, cwd), daemon=True)
        thread.start()
        return run

    def _execute(self, run: StepRun, cmd: list[str], cwd: Path) -> None:
        self._emit(run, f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env={**os.environ, "TF_IN_AUTOMATION": "1"},
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                self._emit(run, line.rstrip("\n"))
            run.returncode = proc.wait()
        except OSError as e:
            self._emit(run, f"[forge] failed to start: {e}")
            run.returncode = -1
        run.status = "succeeded" if run.returncode == 0 else "failed"
        run.finished_at = time.time()
        self._emit(run, f"[forge] step '{run.step}' {run.status} (exit {run.returncode})")
        self._emit(run, None)  # sentinel: stream end
        with self._lock:
            self.history.append(run)
            self.current = None

    def _emit(self, run: StepRun, line: Optional[str]) -> None:
        if line is not None:
            run.lines.append(line)
        for q in list(self._subscribers):
            q.put((run.step, line))

    def subscribe(self) -> Iterator[tuple[str, Optional[str]]]:
        """Yield (step, line) tuples; line=None marks end of a step."""
        q: queue.Queue = queue.Queue()
        # Replay the in-flight run's buffer so late subscribers see everything.
        snapshot: list[tuple[str, Optional[str]]] = []
        with self._lock:
            if self.current is not None:
                snapshot = [(self.current.step, ln) for ln in self.current.lines]
            self._subscribers.append(q)
        try:
            for item in snapshot:
                yield item
            while True:
                try:
                    yield q.get(timeout=30)
                except queue.Empty:
                    yield ("", "")  # keepalive
        finally:
            with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    def state(self) -> dict:
        with self._lock:
            def view(r: StepRun) -> dict:
                return {
                    "step": r.step, "status": r.status, "returncode": r.returncode,
                    "started_at": r.started_at, "finished_at": r.finished_at,
                    "line_count": len(r.lines),
                }
            return {
                "busy": self.busy(),
                "current": view(self.current) if self.current else None,
                "history": [view(r) for r in self.history],
            }
