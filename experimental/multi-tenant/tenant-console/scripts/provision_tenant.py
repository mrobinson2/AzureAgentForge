#!/usr/bin/env python3
"""
provision_tenant.py — End-to-end tenant provisioning for the AzureAgentForge platform.

Steps performed (idempotent where possible):
  1. Validate env / network
  2. Create or look up PaperClip company entity
  3. Note Honcho workspace_id (created lazily on first message)
  4. Provision the standard SMB-tenant agent roster (Orchestrator, Intake, Business, Curator, CostGuardian)
  5. Print next-step manual actions (AGENTS.md upload, channel wiring)

Usage:
    python provision_tenant.py --tenant-id acme-001 --display-name "Acme Plumbing"
    python provision_tenant.py --tenant-id acme-001 --roster minimal     # just Orchestrator + Intake
    python provision_tenant.py --tenant-id acme-001 --roster full        # full 13-agent roster
    python provision_tenant.py --dry-run                                 # show what would happen

Roster choices:
    minimal — Orchestrator + Intake (test/sandbox)
    smb     — Orchestrator + Intake + Business + Curator + CostGuardian (default for SMB clients)
    full    — Full 13-agent platform-owner roster (Infrastructure, Coder, QA, Security, ...)

Requires AGENTS.md upload AFTER this script (use az storage file upload, or your
preferred mechanism). This script creates the database records and skeletons only.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from _lib import (
    auth_header,
    automation_token,
    fail,
    honcho_workspace,
    http,
    info,
    list_agents,
    list_companies,
    ok,
    paperclip_url,
    warn,
)

ROSTERS = {
    "minimal": [
        {"name": "Orchestrator", "role": "orchestrator", "model_tier": "standard"},
        {"name": "Intake",  "role": "receptionist", "model_tier": "cheap"},
    ],
    "smb": [
        {"name": "Orchestrator",    "role": "orchestrator",     "model_tier": "standard"},
        {"name": "Intake",     "role": "receptionist",     "model_tier": "cheap"},
        {"name": "Business",    "role": "smb_ops",          "model_tier": "standard"},
        {"name": "Curator", "role": "memory_curator",   "model_tier": "cheap"},
        {"name": "CostGuardian",     "role": "cost_watchdog",    "model_tier": "cheap"},
    ],
    "full": [
        {"name": "Orchestrator",    "role": "orchestrator",     "model_tier": "premium"},
        {"name": "Intake",     "role": "receptionist",     "model_tier": "cheap"},
        {"name": "Strategy",     "role": "strategy",         "model_tier": "premium"},
        {"name": "Coder",     "role": "code",             "model_tier": "standard"},
        {"name": "Infrastructure",     "role": "infrastructure",   "model_tier": "standard"},
        {"name": "Curator", "role": "memory_curator",   "model_tier": "cheap"},
        {"name": "CostGuardian",     "role": "cost_watchdog",    "model_tier": "cheap"},
        {"name": "Planner",      "role": "planning",         "model_tier": "standard"},
        {"name": "QA",    "role": "qa",               "model_tier": "cheap"},
        {"name": "Security",    "role": "security",         "model_tier": "standard"},
        {"name": "Researcher",   "role": "research",         "model_tier": "premium"},
        {"name": "Business",    "role": "smb_ops",          "model_tier": "standard"},
        {"name": "Coach",    "role": "career",           "model_tier": "standard"},
        {"name": "Psychology", "role": "psychology",       "model_tier": "standard"},
    ],
}


def find_company(tenant_id: str) -> Optional[Dict[str, Any]]:
    for c in list_companies():
        if c.get("id") == tenant_id or c.get("slug") == tenant_id:
            return c
    return None


def create_company(tenant_id: str, display_name: str, dry_run: bool) -> Dict[str, Any]:
    if dry_run:
        info(f"[dry-run] would POST /api/companies with id={tenant_id} name={display_name!r}")
        return {"id": tenant_id, "name": display_name, "_dry_run": True}
    res = http(
        "POST",
        f"{paperclip_url()}/api/companies",
        body={"id": tenant_id, "name": display_name},
        headers=auth_header(),
    )
    if res["error"]:
        raise RuntimeError(f"create_company failed: {res['status']} {res['text'][:200]}")
    return res["json"] or {}


def create_agent(company_id: str, spec: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    if dry_run:
        info(f"[dry-run] would create agent {spec['name']} (role={spec['role']})")
        return {"name": spec["name"], "_dry_run": True}
    res = http(
        "POST",
        f"{paperclip_url()}/api/companies/{company_id}/agents",
        body={
            "name": spec["name"],
            "role": spec["role"],
            "modelTier": spec["model_tier"],
        },
        headers=auth_header(),
    )
    if res["error"]:
        raise RuntimeError(f"create_agent failed for {spec['name']}: {res['status']} {res['text'][:200]}")
    return res["json"] or {}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tenant-id", required=True, help="Tenant slug (used as company_id and Honcho workspace prefix)")
    p.add_argument("--display-name", default=None, help="Human-readable company name")
    p.add_argument("--roster", choices=list(ROSTERS.keys()), default="smb")
    p.add_argument("--dry-run", action="store_true", help="Show planned actions without creating anything")
    p.add_argument("--json", action="store_true", help="Emit JSON summary")
    args = p.parse_args()

    summary: Dict[str, Any] = {
        "tenant_id": args.tenant_id,
        "roster": args.roster,
        "honcho_workspace": args.tenant_id,
        "actions": [],
    }
    display_name = args.display_name or args.tenant_id

    try:
        # 1. Auth check
        token = automation_token()
        info(f"automation token minted ({len(token)} chars)")

        # 2. Company
        existing = find_company(args.tenant_id)
        if existing:
            ok(f"company {args.tenant_id} already exists; will reuse")
            summary["actions"].append({"company": "existing", "id": existing.get("id")})
            company_id = existing.get("id") or args.tenant_id
        else:
            info(f"creating company {args.tenant_id} ({display_name})")
            created = create_company(args.tenant_id, display_name, args.dry_run)
            ok(f"company created: {created.get('id', args.tenant_id)}")
            summary["actions"].append({"company": "created", "id": created.get("id")})
            company_id = created.get("id") or args.tenant_id

        # 3. Agents
        existing_agents = []
        if not args.dry_run:
            try:
                existing_agents = [a.get("name") for a in list_agents(company_id)]
            except Exception:
                existing_agents = []

        for spec in ROSTERS[args.roster]:
            if spec["name"] in existing_agents:
                ok(f"agent {spec['name']} already exists; skipping")
                summary["actions"].append({"agent": spec["name"], "status": "existing"})
                continue
            try:
                created = create_agent(company_id, spec, args.dry_run)
                ok(f"agent {spec['name']} created (role={spec['role']}, tier={spec['model_tier']})")
                summary["actions"].append({"agent": spec["name"], "status": "created", "id": created.get("id")})
            except Exception as e:
                fail(f"failed to create agent {spec['name']}: {e}")
                summary["actions"].append({"agent": spec["name"], "status": "failed", "error": str(e)})

        # 4. Honcho workspace note
        info(f"Honcho workspace_id will be: {args.tenant_id}")
        info(f"  (created lazily on first peer/session/message; no separate provisioning step)")

        # 5. Manual next steps
        next_steps = [
            f"Upload AGENTS.md for each agent to: companies/{company_id}/agents/<agent-id>/AGENTS.md",
            "Wire channel adapter (Telegram bot token, Twilio number, Slack app, etc.)",
            "Run smoke test: python honcho_memory_probe.py --peer test-user --session probe-1",
            "Run delegation test: have Orchestrator receive a 'have Coder write hello' task",
            "Add tenant to billing tracker if applicable",
        ]
        summary["next_steps"] = next_steps

        print()
        info("Provisioning complete. Manual next steps:")
        for i, step in enumerate(next_steps, 1):
            print(f"  {i}. {step}")

    except Exception as e:
        fail(f"provisioning aborted: {e}")
        summary["error"] = str(e)
        if args.json:
            print(json.dumps(summary, indent=2))
        return 1

    if args.json:
        print()
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
