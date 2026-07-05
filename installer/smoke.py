"""Post-deploy smoke verdict for the reference deploy pipeline.

Pure, framework-free pass/fail logic plus a thin CLI. The bash driver
(``scripts/smoke-test.sh``) collects ``az containerapp show`` JSON for each
deployed app and optional HTTP probe results, assembles them into one JSON
document, and pipes it here. Keeping the verdict in plain Python means it is
unit-testable offline with no Azure calls — the same split as
``installer.core``/``installer.detect_destroy``.

Unlike ``installer.detect_destroy`` (which always exits 0 and lets the job
``if:`` conditions gate), smoke is a real gate: an unhealthy or unreadable
result exits non-zero so the deploy run goes red.

Input document (stdin, or a path passed as the single argument)::

    {
      "expected": ["ca-paperclip-dev", "ca-hermes-dev"],
      "apps": [
        {"name": "ca-paperclip-dev", "show": { ...az containerapp show JSON... }},
        {"name": "ca-hermes-dev",    "show": { ... }}
      ],
      "http": [
        {"name": "paperclip-ui", "url": "https://...", "status": 200}
      ]
    }

An app is healthy when its ``show`` object reports
``properties.provisioningState == "Succeeded"`` and its ``runningStatus`` is
not in :data:`RUNNING_BAD`. A missing ``show`` block fails that app
(fail-closed: we deployed it, so we expect to see it). Any name in
``expected`` with no matching ``apps`` entry fails. An HTTP probe passes on a
2xx/3xx status.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

# Container Apps provisioningState we require after a successful deploy.
PROVISIONING_OK = "Succeeded"

# runningStatus values that mean the app is not serving. Container Apps that
# scale to zero still report "Running" (replica count is separate), so only
# explicit failure/stopped states fail the check.
RUNNING_BAD = {"Failed", "Stopped", "Suspended"}


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass
class SmokeReport:
    ok: bool
    checks: List[CheckResult] = field(default_factory=list)

    def failures(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.ok]


def _props(show: dict) -> dict:
    """Container app properties live under .properties; tolerate either shape."""
    if not isinstance(show, dict):
        return {}
    props = show.get("properties")
    return props if isinstance(props, dict) else show


def check_app(entry: dict) -> CheckResult:
    """Verdict for one ``{"name", "show"}`` entry."""
    name = (entry or {}).get("name") or "<unnamed>"
    show = (entry or {}).get("show")
    if not isinstance(show, dict) or not show:
        # Fail-closed: an app we expected to read returned nothing.
        return CheckResult(name, False, "no 'az containerapp show' output (app missing or unreadable)")
    props = _props(show)
    state = props.get("provisioningState")
    running = props.get("runningStatus")
    if state != PROVISIONING_OK:
        return CheckResult(name, False, f"provisioningState={state!r} (want {PROVISIONING_OK!r})")
    if running in RUNNING_BAD:
        return CheckResult(name, False, f"runningStatus={running!r}")
    suffix = f", runningStatus={running}" if running else ""
    return CheckResult(name, True, f"provisioningState={state}{suffix}")


def check_http(entry: dict) -> CheckResult:
    """Verdict for one HTTP probe ``{"name", "url", "status"}`` entry."""
    name = (entry or {}).get("name") or (entry or {}).get("url") or "<http>"
    status = (entry or {}).get("status")
    try:
        code = int(status)
    except (TypeError, ValueError):
        return CheckResult(name, False, f"no/invalid HTTP status ({status!r}) — probe did not complete")
    if 200 <= code < 400:
        return CheckResult(name, True, f"HTTP {code}")
    return CheckResult(name, False, f"HTTP {code}")


def evaluate(payload: dict, expected: Optional[List[str]] = None) -> SmokeReport:
    """Combine app + HTTP + presence checks into one report.

    ``expected`` overrides ``payload['expected']`` when provided. Every expected
    name must appear in ``payload['apps']`` or it fails as missing.
    """
    payload = payload or {}
    apps = payload.get("apps") or []
    https = payload.get("http") or []
    want = expected if expected is not None else (payload.get("expected") or [])

    checks: List[CheckResult] = []
    seen = set()
    for entry in apps:
        res = check_app(entry)
        seen.add(res.name)
        checks.append(res)

    for name in want:
        if name not in seen:
            checks.append(CheckResult(name, False, "expected app not found in deploy outputs"))

    for entry in https:
        checks.append(check_http(entry))

    if not checks:
        # Nothing to assert is itself a failure — a smoke test that checks
        # nothing must not report success.
        checks.append(CheckResult("<smoke>", False, "no apps or probes to check"))

    ok = all(c.ok for c in checks)
    return SmokeReport(ok=ok, checks=checks)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit_output(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def _emit_summary(text: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def _read_payload(argv: List[str]) -> dict:
    if len(argv) >= 2 and argv[1] not in ("-", "/dev/stdin"):
        with open(argv[1], encoding="utf-8") as fh:
            return json.load(fh)
    return json.load(sys.stdin)


def main(argv: List[str]) -> int:
    try:
        payload = _read_payload(argv)
    except (OSError, json.JSONDecodeError) as e:
        # Fail-closed: an unreadable payload means we cannot confirm health.
        print(f"[smoke] could not read probe payload ({e}) — FAILING", file=sys.stderr)
        _emit_output("ok", "false")
        _emit_summary("### ❌ Smoke check could not read deploy probes")
        return 1

    report = evaluate(payload)

    for c in report.checks:
        mark = "✅" if c.ok else "❌"
        print(f"{mark} {c.name}: {c.detail}")

    if report.ok:
        _emit_summary("### ✅ Smoke check passed")
        _emit_summary("\n".join(f"- `{c.name}` — {c.detail}" for c in report.checks))
    else:
        fails = report.failures()
        _emit_summary("### ❌ Smoke check failed")
        _emit_summary(f"{len(fails)} of {len(report.checks)} check(s) failed:\n")
        _emit_summary("\n".join(f"- `{c.name}` — {c.detail}" for c in fails))

    _emit_output("ok", "true" if report.ok else "false")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
