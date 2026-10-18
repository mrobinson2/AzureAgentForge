"""Subject identity for governed memory — the per-person handle behind
erasure and export (docs/design/enhancement-proposals-2026-10.md §4).

A *subject* is the end user a memory is about (not the agent, not the
workspace). Writers pass the raw provider-scoped id (e.g. "teams:<aadObjectId>"
or "slack:<team>/<user>"); the governor stores only a peppered SHA-256 of it in
`documents.internal_metadata.subject_hash`, so the database never holds the raw
identifier and an export/erase request names the hash, not the person.

Pure module: no I/O. The SQL that reads/writes the tag lives in main.py and
admission.py and is covered by the offline FakePool tests.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def pepper() -> str:
    """Deployment pepper. Falls back to the governor key so a deployment
    without SUBJECT_HASH_PEPPER still gets a non-public hash input."""
    return os.environ.get("SUBJECT_HASH_PEPPER") or os.environ.get("GOVERNOR_API_KEY") or ""


def hash_subject(raw: str, pepper_value: str | None = None) -> str:
    """HMAC-SHA256(pepper, normalized raw) as 64 hex chars. Deterministic per
    deployment so the same person always maps to the same hash."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("subject must be non-empty")
    key = (pepper() if pepper_value is None else pepper_value).encode("utf-8")
    return hmac.new(key, raw.lower().encode("utf-8"), hashlib.sha256).hexdigest()


def is_subject_hash(value: str) -> bool:
    return bool(value) and _HEX64.match(value) is not None


def erase_request_id(subject_hash: str, workspace_name: str, day: str) -> str:
    """Two-step erase token: the operator must echo the id the dry run returned
    (same subject, workspace, and UTC day) before anything is deleted."""
    msg = f"{subject_hash}|{workspace_name}|{day}".encode("utf-8")
    return hmac.new(pepper().encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]


def build_export_bundle(subject_hash: str, workspace_name: str, documents: list, sessions: list) -> dict:
    """Pure: assemble the operator-facing export. Rows are dicts/records from
    asyncpg; only governed fields are included, never internal_metadata."""
    def _doc(r):
        return {
            "id": r["id"],
            "content": r["content"],
            "memory_class": r.get("memory_class") if hasattr(r, "get") else r["memory_class"],
            "scope_kind": r["memory_scope_kind"],
            "scope_id": r["memory_scope_id"],
            "source_type": r["source_type"],
            "verification_state": r["verification_state"],
            "created_by_peer": r["created_by_peer"],
            "created_at": str(r["created_at"]) if r["created_at"] is not None else None,
            "expires_at": str(r["expires_at"]) if r["expires_at"] is not None else None,
        }

    def _sess(r):
        return {
            "id": r["id"],
            "session_id": r["session_id"],
            "content": r["content"],
            "created_by_peer": r["created_by_peer"],
            "expires_at": str(r["expires_at"]) if r["expires_at"] is not None else None,
        }

    return {
        "subject_hash": subject_hash,
        "workspace_name": workspace_name,
        "documents": [_doc(r) for r in documents],
        "session_memories": [_sess(r) for r in sessions],
        "counts": {"documents": len(documents), "session_memories": len(sessions)},
    }
