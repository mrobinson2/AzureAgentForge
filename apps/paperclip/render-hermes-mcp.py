#!/usr/bin/env python3
"""render-hermes-mcp.py — render the Hermes `mcp_servers:` config block from the
platform MCP registry (agents/mcp-registry.json).

Called by write-hermes-config.sh at container boot, AFTER the base config
heredoc, and appended to config.yaml. Stdlib only (json), so it runs on the
image's python3 with no site-packages assumptions.

Rules (the governance contract — see docs/design/enhancement-proposals-2026-10.md):
  * Only servers named in AAF_MCP_SERVERS (comma-separated) are rendered.
    Unset/empty => nothing rendered => the MCP plane is inert (off by default).
  * A name that is not in the registry is a loud error on stderr and is
    skipped; it never silently renders something else.
  * A server with `auth.secret_env` is rendered only when that exact env var
    is set. Nothing else from the environment is read — in particular the
    router key never reaches an MCP header.
  * Each server carries its registry `tools.include` allow-list verbatim;
    Hermes enforces it per server (only those tools are registered).

Usage: render-hermes-mcp.py --registry PATH [--servers a,b] [--env-from-process]
Prints YAML to stdout; exit 0 even when nothing is rendered (boot must not
fail on an optional feature); exit 2 on a malformed registry.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _q(s: str) -> str:
    """YAML double-quoted scalar."""
    return json.dumps(str(s))


def render(registry: dict, requested: list[str], env: dict) -> tuple[str, list[str]]:
    """Pure: (yaml_text, warnings). yaml_text is '' when nothing is rendered."""
    servers = registry.get("servers") or {}
    warnings: list[str] = []
    out: list[str] = []
    for name in requested:
        name = name.strip()
        if not name:
            continue
        srv = servers.get(name)
        if not isinstance(srv, dict):
            warnings.append(f"MCP server '{name}' is not in the registry — skipped")
            continue
        auth = srv.get("auth") or {}
        secret_val = None
        if auth:
            secret_env = auth.get("secret_env", "")
            secret_val = (env.get(secret_env) or "").strip()
            if not secret_val or secret_val == "__unset__":
                warnings.append(f"MCP server '{name}' skipped: {secret_env} unset (Key Vault secret "
                                f"'{auth.get('key_vault_secret', '?')}' not provisioned)")
                continue
        block = [f"  {name}:"]
        if srv.get("transport") == "http":
            block.append(f"    url: {_q(srv['url'])}")
            if auth:
                header = auth.get("header") or "Authorization"
                scheme = auth.get("scheme") or "Bearer"
                value = secret_val if scheme == "raw" else f"{scheme} {secret_val}"
                block.append("    headers:")
                block.append(f"      {header}: {_q(value)}")
        else:
            block.append(f"    command: {_q(srv['command'])}")
            args = srv.get("args") or []
            block.append("    args: [" + ", ".join(_q(a) for a in args) + "]")
            if auth:
                block.append("    env:")
                block.append(f"      {auth.get('env_var') or auth['secret_env']}: {_q(secret_val)}")
        if srv.get("timeout"):
            block.append(f"    timeout: {srv['timeout']}")
        tools = srv.get("tools") or {}
        block.append("    tools:")
        block.append("      include: [" + ", ".join(_q(t) for t in tools.get("include", [])) + "]")
        if tools.get("exclude"):
            block.append("      exclude: [" + ", ".join(_q(t) for t in tools["exclude"]) + "]")
        for k in ("prompts", "resources"):
            if k in tools:
                block.append(f"      {k}: {'true' if tools[k] else 'false'}")
        out.extend(block)
    if not out:
        return "", warnings
    header = [
        "# MCP servers — rendered by render-hermes-mcp.py from agents/mcp-registry.json;",
        "# only the servers named in AAF_MCP_SERVERS, each with its registry allow-list.",
        "mcp_servers:",
    ]
    return "\n".join(header + out) + "\n", warnings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--servers", default=None, help="comma list; default $AAF_MCP_SERVERS")
    args = ap.parse_args(argv)
    try:
        with open(args.registry, encoding="utf-8") as fh:
            registry = json.load(fh)
        if not isinstance(registry.get("servers"), dict):
            raise ValueError("registry has no 'servers' object")
    except Exception as exc:  # noqa: BLE001
        print(f"[render-hermes-mcp] ERROR: cannot read registry {args.registry}: {exc}", file=sys.stderr)
        return 2
    raw = args.servers if args.servers is not None else os.environ.get("AAF_MCP_SERVERS", "")
    requested = [s for s in raw.split(",") if s.strip()]
    if not requested:
        print("[render-hermes-mcp] AAF_MCP_SERVERS unset — no MCP servers rendered", file=sys.stderr)
        return 0
    text, warnings = render(registry, requested, dict(os.environ))
    for w in warnings:
        print(f"[render-hermes-mcp] WARNING: {w}", file=sys.stderr)
    if text:
        sys.stdout.write(text)
        rendered = [l.strip()[:-1] for l in text.splitlines() if l.startswith("  ") and l.strip().endswith(":") and not l.startswith("    ")]
        print(f"[render-hermes-mcp] rendered MCP servers: {', '.join(rendered)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
