#!/usr/bin/env python3
"""Validate every agent profile in profiles/ against profile.schema.json, the
MCP registry against mcp-registry.schema.json, and every profile's
`mcp_servers` grant against the registry (an unregistered grant is an error,
never a silent no-op)."""
import json, pathlib, sys
import yaml
from jsonschema import Draft202012Validator

HERE = pathlib.Path(__file__).resolve().parent


def load_registry(path=None):
    path = path or (HERE / "mcp-registry.json")
    data = json.loads(path.read_text())
    schema = json.loads((HERE / "mcp-registry.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    return data, [f"{path.name}: {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]


def grant_errors(profile_name, data, registry):
    """Every `mcp_servers` entry must name a registered server."""
    servers = set((registry or {}).get("servers") or {})
    out = []
    for name in (data or {}).get("mcp_servers") or []:
        if name not in servers:
            out.append(f"{profile_name}: mcp_servers grant '{name}' is not in agents/mcp-registry.json "
                       f"(registered: {', '.join(sorted(servers)) or 'none'})")
    return out


def main():
    schema = json.loads((HERE / "profile.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    profiles = sorted((HERE / "profiles").glob("*.yaml"))
    if not profiles:
        print("No profiles found.")
        return 1

    registry, reg_errors = load_registry()
    for msg in reg_errors:
        print(msg)

    failed = 0
    for f in profiles:
        data = yaml.safe_load(f.read_text())
        errors = [e.message for e in sorted(validator.iter_errors(data), key=lambda e: e.path)]
        errors += grant_errors(f.name, data, registry)
        if errors:
            failed += 1
            for msg in errors:
                print(f"{f.name}: {msg}")
    if failed or reg_errors:
        print(f"\nFAIL: {failed}/{len(profiles)} profiles invalid; {len(reg_errors)} registry error(s).")
        return 1
    print(f"OK: {len(profiles)} profiles valid; {len(registry['servers'])} MCP server(s) registered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
