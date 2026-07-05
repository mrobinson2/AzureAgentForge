"""Turn Findings into PaperClip issues (the I/O half).

Files a deduped issue per finding via the automation/admin JWT pattern
(camelCase payload — Zod strips snake_case). Records a `watchdog_filed`
agent_events row so the watchdog's own actions are auditable on the same spine
it watches.

Network calls live here so detectors.py stays pure and testable. This module's
own logic (payload shape, dedup-store round-trip) is tested with a stub poster.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Callable, Optional

from .detectors import Finding

ISSUE_LABELS = {"critical": "priority:critical", "high": "priority:high",
                "medium": "priority:medium"}


def build_issue_payload(f: Finding, company_id: str) -> dict:
    """The camelCase issue body the watchdog files. Pure — unit-tested."""
    body = (
        f"**Auto-filed by the platform watchdog.**\n\n"
        f"{f.summary}\n\n"
        f"**Severity:** {f.severity}\n"
        f"**Recommended owner:** {f.recommended_owner}\n"
        f"**Signature:** `{f.dedup_key()}` (re-occurrences update, not duplicate)\n\n"
        f"### Evidence\n```json\n{json.dumps(f.evidence, indent=2)}\n```\n"
    )
    return {
        "title": f"[watchdog] {f.title}",
        "description": body,
        "status": "todo",
        "metadata": {"watchdogSignature": f.dedup_key(), "severity": f.severity},
    }


def file_finding(f: Finding, *, base_url: str, company_id: str, jwt: str,
                 poster: Optional[Callable] = None) -> dict:
    """File one finding. `poster` overridable for tests (defaults to urllib POST)."""
    payload = build_issue_payload(f, company_id)
    url = f"{base_url}/api/companies/{company_id}/issues"
    if poster is not None:
        return poster(url, payload, jwt)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {jwt}")
    req.add_header("Origin", base_url)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Automation-Sub", "watchdog")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or "{}")
