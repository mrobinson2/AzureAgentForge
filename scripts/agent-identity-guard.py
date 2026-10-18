#!/usr/bin/env python3
"""agent-identity-guard.py — config-time guard for per-agent Azure identities.

Each agent profile may declare `azure_identity` (agents/profile.schema.json):
an opt-in user-assigned managed identity with an explicit list of role
assignments that Terraform materializes. This guard is the blast-radius
check the design calls for: it refuses, at review time, any grant that would
hand an agent broad or privileged Azure rights, so a prompt-injected or
runaway agent can never hold more than its job needs.

Rules (deny wins):
  * Forbidden anywhere: Owner, Contributor, User Access Administrator,
    Role Based Access Control Administrator, Key Vault Administrator,
    Key Vault Secrets Officer, Key Vault Crypto Officer.
  * A write-capable role (anything not in the read-only allow-list) needs
    `allow_write: true` AND a non-trivial reason.
  * Scope must be resource_group or key_vault (schema-enforced); there is
    no subscription or management-group scope at all.

Usage: python scripts/agent-identity-guard.py [--profiles-dir DIR]
Exit 0 pass, 1 violations, 2 fatal (unreadable config).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

FORBIDDEN_ROLES = {
    "owner", "contributor", "user access administrator",
    "role based access control administrator", "key vault administrator",
    "key vault secrets officer", "key vault crypto officer",
}
READ_ONLY_ROLES = {
    "reader", "key vault reader", "key vault secrets user", "monitoring reader",
    "log analytics reader", "acrpull", "cognitive services openai user",
    "key vault crypto user",
}
MIN_REASON = 10


def check_profile(slug: str, data: dict) -> list[str]:
    """Pure: violations for one profile (empty = pass)."""
    ident = (data or {}).get("azure_identity")
    if not ident:
        return []
    out: list[str] = []
    if not ident.get("enabled", False):
        return out
    if len(str(ident.get("reason") or "").strip()) < MIN_REASON:
        out.append(f"{slug}: azure_identity.reason must explain why this role needs its own identity")
    ras = ident.get("role_assignments") or []
    if not ras:
        out.append(f"{slug}: azure_identity.enabled without any role_assignments (an identity with no grants is pointless)")
    for i, ra in enumerate(ras):
        role = str(ra.get("role") or "").strip()
        key = role.lower()
        scope = ra.get("scope")
        where = f"{slug}: role_assignments[{i}] '{role}' @ {scope}"
        if scope not in ("resource_group", "key_vault"):
            out.append(f"{where}: scope must be resource_group or key_vault")
        if key in FORBIDDEN_ROLES:
            out.append(f"{where}: forbidden role — an agent may never hold it at any scope")
            continue
        if key not in READ_ONLY_ROLES:
            if not ra.get("allow_write"):
                out.append(f"{where}: write-capable role requires allow_write: true")
            if len(str(ra.get("reason") or "").strip()) < MIN_REASON:
                out.append(f"{where}: write-capable role requires a reason")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles-dir", default=str(Path(__file__).resolve().parents[1] / "agents" / "profiles"))
    args = ap.parse_args(argv)
    pdir = Path(args.profiles_dir)
    files = sorted(pdir.glob("*.yaml"))
    if not files:
        print(f"agent-identity-guard: no profiles under {pdir}", file=sys.stderr)
        return 2
    violations: list[str] = []
    declared = 0
    for f in files:
        try:
            data = yaml.safe_load(f.read_text()) or {}
        except Exception as exc:  # noqa: BLE001
            print(f"agent-identity-guard: cannot parse {f.name}: {exc}", file=sys.stderr)
            return 2
        if (data.get("azure_identity") or {}).get("enabled"):
            declared += 1
        violations += check_profile(f.stem, data)
    for v in violations:
        print(f"VIOLATION {v}")
    if violations:
        print(f"agent-identity-guard: FAIL — {len(violations)} violation(s) across {len(files)} profiles")
        return 1
    print(f"agent-identity-guard: OK — {declared} role(s) declare an identity, none over-privileged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
