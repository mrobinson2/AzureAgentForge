#!/usr/bin/env python3
"""derive-decision-signing-secret.py — domain-separated subkey for PaperClip's
decision signing, derived from an already-mounted root secret.

Upstream PaperClip (>= v2026.1001.0) signs approval decisions with
PAPERCLIP_DECISION_SIGNING_SECRET. When that env is unset it auto-generates a
key file under <home>/instances/<id>/secrets and refuses to start unless the
directory is owned by the process user — which the Azure File Share mount can
never satisfy (uid/gid are fixed at mount time).

The preferred fix is a dedicated Key Vault secret (paperclip.tf,
paperclip_decision_signing_secret_enabled). Until that secret is provisioned,
this script derives a stable, purpose-bound key from BETTER_AUTH_SECRET:

    HMAC-SHA256(key = root secret, msg = "aaf:paperclip:decision-signing:v1")

Properties: deterministic across restarts (signatures stay valid), 64 hex
chars (>= the 32 upstream requires), never written to disk, and a compromise
of the root secret already compromises every session — so no new trust is
created. Stdlib only.

Usage: derive-decision-signing-secret.py   (reads $ROOT_SECRET_ENV, default
BETTER_AUTH_SECRET; prints the derived key; exit 1 if the root is absent or
shorter than 32 chars).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys

LABEL = b"aaf:paperclip:decision-signing:v1"
MIN_ROOT_LEN = 32


def derive(root: str, label: bytes = LABEL) -> str:
    """Pure: HMAC-SHA256(root, label) as 64 hex chars."""
    if len(root) < MIN_ROOT_LEN:
        raise ValueError(f"root secret must be at least {MIN_ROOT_LEN} characters")
    return hmac.new(root.encode("utf-8"), label, hashlib.sha256).hexdigest()


def main() -> int:
    env_name = os.environ.get("ROOT_SECRET_ENV", "BETTER_AUTH_SECRET")
    root = (os.environ.get(env_name) or "").strip()
    if not root or root == "__unset__":
        print(f"[derive-decision-signing-secret] {env_name} unset — cannot derive", file=sys.stderr)
        return 1
    try:
        sys.stdout.write(derive(root))
    except ValueError as exc:
        print(f"[derive-decision-signing-secret] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
