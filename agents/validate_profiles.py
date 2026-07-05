#!/usr/bin/env python3
"""Validate every agent profile in profiles/ against profile.schema.json."""
import json, pathlib, sys
import yaml
from jsonschema import Draft202012Validator

HERE = pathlib.Path(__file__).resolve().parent

def main():
    schema = json.loads((HERE / "profile.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    profiles = sorted((HERE / "profiles").glob("*.yaml"))
    if not profiles:
        print("No profiles found.")
        return 1
    failed = 0
    for f in profiles:
        data = yaml.safe_load(f.read_text())
        errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
        if errors:
            failed += 1
            for e in errors:
                print(f"{f.name}: {e.message}")
    if failed:
        print(f"\nFAIL: {failed}/{len(profiles)} profiles invalid.")
        return 1
    print(f"OK: {len(profiles)} profiles valid.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
