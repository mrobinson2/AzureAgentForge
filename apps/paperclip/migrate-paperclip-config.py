#!/usr/bin/env python3
"""migrate-paperclip-config.py — keep <PAPERCLIP_HOME>/instances/default/config.json
valid for the pinned upstream and in sync with the container env.

Upstream PaperClip >= v2026.1001.0 validates config.json against a strict
schema (packages/shared/src/config-schema.ts): `$meta`, `database`, `logging`,
`server` and `telemetry` are required objects. The flat file earlier AAF images
wrote ({instanceId, publicUrl, allowedHostnames, deploymentMode,
deploymentExposure}) fails that validation and the server refuses to start.
The file lives on the persistent Azure File Share, so every existing
deployment carries the legacy shape.

Three cases, all idempotent:
  missing  -> write a v1 config from env
  legacy   -> back it up (config.json.legacy-<utc>) and rewrite as v1,
              preserving its values where env does not override them
  v1       -> sync the env-controlled fields; rewrite only if something changed

Upstream precedence (server/src/config.ts): env wins for deploymentMode,
exposure, host, port, publicUrl and allowedHostnames; `database.mode` comes
ONLY from the file, so it is always written as "postgres" (DATABASE_URL stays
in env and is never written here). Secrets are never written.

Usage: migrate-paperclip-config.py <config.json path>   (reads env). Exit 0 on
success; exit 1 with a WARNING on any failure — the entrypoint logs and
continues so the server's own error, if any, is the one operators see.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

V1_SOURCE = "configure"


def _hostnames(raw: str | None) -> list[str]:
    return [h.strip() for h in (raw or "").split(",") if h.strip()]


def build_v1(legacy: dict | None, env: dict, instance_root: str, now: str) -> dict:
    """Pure: a schema-valid v1 config from a legacy flat file (or None) + env."""
    legacy = legacy or {}
    deployment_mode = env.get("PAPERCLIP_DEPLOYMENT_MODE") or legacy.get("deploymentMode") or "authenticated"
    exposure = env.get("PAPERCLIP_DEPLOYMENT_EXPOSURE") or legacy.get("deploymentExposure") or "private"
    hostnames = _hostnames(env.get("PAPERCLIP_ALLOWED_HOSTNAMES")) or _hostnames(legacy.get("allowedHostnames"))
    public_url = env.get("PAPERCLIP_PUBLIC_URL") or legacy.get("publicUrl")
    cfg: dict = {
        "$meta": {"version": 1, "updatedAt": now, "source": V1_SOURCE},
        "database": {"mode": "postgres"},
        "logging": {"mode": "file", "logDir": f"{instance_root}/logs"},
        "server": {
            "deploymentMode": deployment_mode,
            "exposure": exposure,
            "allowedHostnames": hostnames,
        },
        "telemetry": {},
    }
    if public_url:
        cfg["auth"] = {"baseUrlMode": "explicit", "publicBaseUrl": public_url, "disableSignUp": False}
    return cfg


def sync_v1(cfg: dict, env: dict) -> tuple[dict, list[str]]:
    """Pure: apply env-controlled fields to an existing v1 config. Returns
    (config, changed_keys)."""
    changed: list[str] = []
    server = cfg.setdefault("server", {})
    for key, var in (("deploymentMode", "PAPERCLIP_DEPLOYMENT_MODE"), ("exposure", "PAPERCLIP_DEPLOYMENT_EXPOSURE")):
        val = env.get(var)
        if val and server.get(key) != val:
            server[key] = val
            changed.append(f"server.{key}")
    hosts = _hostnames(env.get("PAPERCLIP_ALLOWED_HOSTNAMES"))
    if hosts and server.get("allowedHostnames") != hosts:
        server["allowedHostnames"] = hosts
        changed.append("server.allowedHostnames")
    public_url = env.get("PAPERCLIP_PUBLIC_URL")
    if public_url:
        auth = cfg.setdefault("auth", {})
        if auth.get("publicBaseUrl") != public_url:
            auth["publicBaseUrl"] = public_url
            auth.setdefault("baseUrlMode", "explicit")
            auth.setdefault("disableSignUp", False)
            changed.append("auth.publicBaseUrl")
    db = cfg.setdefault("database", {})
    if db.get("mode") != "postgres":
        db["mode"] = "postgres"
        changed.append("database.mode")
    for key, default in (("logging", {"mode": "file"}), ("telemetry", {})):
        if not isinstance(cfg.get(key), dict):
            cfg[key] = dict(default)
            changed.append(key)
    return cfg, changed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: migrate-paperclip-config.py <config.json>", file=sys.stderr)
        return 1
    path = argv[1]
    env = dict(os.environ)
    instance_root = os.path.dirname(path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        if not os.path.exists(path):
            cfg = build_v1(None, env, instance_root, now)
            os.makedirs(instance_root, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            print(f"[migrate-paperclip-config] created v1 config at {path}", file=sys.stderr)
            return 0
        with open(path, encoding="utf-8") as fh:
            current = json.load(fh)
        if not isinstance(current, dict):
            raise ValueError("config.json is not a JSON object")
        if not isinstance(current.get("$meta"), dict):
            backup = f"{path}.legacy-{now.replace(':', '')}"
            os.replace(path, backup)
            cfg = build_v1(current, env, instance_root, now)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            print(f"[migrate-paperclip-config] migrated legacy config to v1 (backup: {os.path.basename(backup)})", file=sys.stderr)
            return 0
        cfg, changed = sync_v1(current, env)
        if changed:
            cfg["$meta"]["updatedAt"] = now
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            print(f"[migrate-paperclip-config] synced from env: {', '.join(changed)}", file=sys.stderr)
        else:
            print("[migrate-paperclip-config] config.json already in sync", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001 — never abort boot from here
        print(f"[migrate-paperclip-config] WARNING: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
