"""Forge Console preflight — offline prerequisite + operator-gate report.

Run it:  ./forge --check      (or:  python -m installer.preflight)

Prints, WITHOUT starting the server or provisioning anything:
  * which tools each deploy path needs and whether they're present,
  * a readiness verdict for the Azure path and the local Docker path,
  * the operator gates (human sign-off points) a real deploy stops at.

Imports only :mod:`installer.core` (standard library), so it runs under the
system Python without the console's virtualenv, and makes no cloud call beyond
the read-only version/login probes ``core.run_checks`` already performs.

Exit code: 0 if at least one deploy path is ready, 1 if neither is — handy for
scripting a gate ("is this machine ready to deploy?") in CI or a Makefile.
"""

from __future__ import annotations

import sys

from . import core

# The human approval points a real deploy stops at. Kept here in ONE place so
# `--check`, the web UI's "Operator gates" card, and AI-ASSISTED-SETUP.md all
# describe the SAME gates in the SAME words.
OPERATOR_GATES: list[tuple[str, str]] = [
    ("Subscription / billing gate",
     "You pick the Azure subscription; `apply` creates real, billable resources "
     "(see docs/cost.md)."),
    ("Secrets-in-Key-Vault gate",
     "You seed provider keys + Postgres strings into Key Vault "
     "(scripts/bootstrap.sh, scripts/seed-keyvault.sh) — never on argv, never in a browser."),
    ("Environment-name confirmation gate",
     "`apply` and `destroy` require typing the environment name before they run."),
    ("Destroy-approval gate",
     "If the saved plan would delete or replace a resource, a second prompt "
     "requires typing 'approve-destroy'. Pure adds/updates apply without it."),
    ("CI/CD scaffold-apply gate",
     "Wiring the deploy pipeline mutates Azure identity/RBAC + GitHub config, "
     "so it requires typing 'scaffold-apply'."),
]

# Tools that make each deploy path viable.
_AZURE_TOOLS = ("terraform", "az", "azure_login")
_LOCAL_TOOLS = ("docker",)


def _mark(found: bool) -> str:
    return "✔" if found else "✘"  # ✔ / ✘


def format_report(checks: dict) -> str:
    """Render the prerequisite table + a readiness verdict for each path."""
    lines = ["Prerequisites", "-" * 62]
    for key in ("terraform", "az", "azure_login", "docker"):
        info = checks.get(key)
        if not info:
            continue
        req = info.get("required_for", "")
        detail = info.get("detail", "")
        lines.append(f"  [{_mark(bool(info.get('found')))}] {key:<12} ({req:<6}) {detail}")

    az_ready = all(checks.get(k, {}).get("found") for k in _AZURE_TOOLS)
    local_ready = all(checks.get(k, {}).get("found") for k in _LOCAL_TOOLS)
    lines += ["", "Readiness", "-" * 62]
    lines.append(f"  [{_mark(az_ready)}] Azure path   — needs terraform + az + az login")
    lines.append(f"  [{_mark(local_ready)}] Local path   — needs Docker running")
    return "\n".join(lines)


def format_gates() -> str:
    """Render the operator-gate reference (human sign-off points)."""
    lines = ["Operator gates — where you sign off", "-" * 62]
    for name, desc in OPERATOR_GATES:
        lines.append(f"  * {name}")
        lines.append(f"      {desc}")
    return "\n".join(lines)


def is_ready(checks: dict) -> bool:
    az_ready = all(checks.get(k, {}).get("found") for k in _AZURE_TOOLS)
    local_ready = all(checks.get(k, {}).get("found") for k in _LOCAL_TOOLS)
    return bool(az_ready or local_ready)


def main(argv: list[str] | None = None) -> int:
    checks = core.run_checks()
    print(format_report(checks))
    print()
    print(format_gates())
    ready = is_ready(checks)
    print()
    if ready:
        print("Preflight OK — at least one deploy path is ready. Start the console: ./forge")
    else:
        print("Preflight: no deploy path is ready yet. Install the tools above "
              "(and run 'az login' for the Azure path), then re-run ./forge --check")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
