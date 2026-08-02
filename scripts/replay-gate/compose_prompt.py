#!/usr/bin/env python3
"""compose_prompt.py — prompt composer for the Behavioural Replay Gate.

WHAT "COMPOSE" MEANS HERE (read this before extending it)
-----------------------------------------------------------
AzureAgentForge has no build step for agent prompts. Per agents/README.md,
each `agents/profiles/<role>.AGENTS.md` file *is* "the actual prompt
prepended to every task the role runs" — verbatim, minus its YAML
frontmatter. There is no deploy script here that concatenates includes or
substitutes placeholders (contrast this with a platform that composes a
persona from a shared preamble + role body + runtime values — this repo does
not do that today).

So "composing" a candidate prompt in AAF is NOT the multi-step assembly you
might expect from the phrase. It is:

  1. Locate `agents/profiles/<role>.AGENTS.md` for the given role slug.
  2. Validate its YAML frontmatter — present, first thing in the file, valid
     YAML, only the five house-convention keys (role/voice_id/color/emoji/
     vibe — see agents/templates/AGENTS.template.md), and a `role` value
     that is a well-formed slug matching the filename.
  3. Strip the frontmatter. What remains IS the composed, deployable prompt
     — there is nothing else to resolve.
  4. Fail loudly, not silently, on every validation defect above, and on any
     leftover `{{PLACEHOLDER}}` token in the body (forward-looking: no
     current profile has one, but this catches the day a future include or
     templating mechanism lands and partially fails).
  5. Emit a byte/hash manifest and labeled ESTIMATED token counts.

Call this a **validating canonicalizer** rather than a composer-in-the D9
sense: it proves "these are the exact bytes that would ship" and catches
structural defects in getting there, but it does not invent an include or
placeholder mechanism this repo does not have. If AAF ever gains a real
compose step (a shared preamble, a templating pass), extend `compose()` here
— this module is deliberately the one place prompt bytes get produced for
both this gate and any future deploy tooling, so they cannot silently
diverge.

FOUND WHILE BUILDING THIS: `agents/profiles/generalist.yaml` shipped with no
matching `generalist.AGENTS.md` — a role with a machine-readable contract
(model tier, toolsets, reporting line) and zero governance prompt. `compose()`
fails loudly on that (see CompositionError below) instead of the role quietly
running with no scope guard, no disposition protocol, nothing. Fixed in its
own commit; see docs/design/prompt-replay-gate.md.

Usage:
  # Compose one role, print composed bytes to stdout
  python3 scripts/replay-gate/compose_prompt.py compose cost-guardian

  # Compose the whole roster (derived from agents/profiles/*.yaml) into a
  # directory of <role>.composed.md files + a manifest.json
  python3 scripts/replay-gate/compose_prompt.py compose-many --out-dir /tmp/out

  # Print a Markdown table of byte + estimated token counts for a manifest
  python3 scripts/replay-gate/compose_prompt.py report --manifest /tmp/out/manifest.json

Exit codes: 0 success, 1 one-or-more compositions failed loudly, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILES_DIR = REPO_ROOT / "agents" / "profiles"

# House convention, see agents/templates/AGENTS.template.md's frontmatter
# guidance comment: "Keep frontmatter to these five keys."
ALLOWED_FRONTMATTER_KEYS = {"role", "voice_id", "color", "emoji", "vibe"}
REQUIRED_FRONTMATTER_KEYS = {"role"}

FRONTMATTER_AT_START_RE = re.compile(r"\A---[ \t]*\r?\n(.*?\r?\n)---[ \t]*\r?\n?", re.DOTALL)
FRONTMATTER_ANYWHERE_RE = re.compile(r"^---\s*$", re.MULTILINE)
PLACEHOLDER_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
ROLE_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")

PROFILE_SUFFIX = ".AGENTS.md"


class CompositionError(Exception):
    """A loud, specific composition failure. Never caught and papered over."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _yaml():
    import yaml  # local import: keep `--help` usable without PyYAML installed
    return yaml


def parse_roster(profiles_dir: Path = DEFAULT_PROFILES_DIR) -> list[str]:
    """Single source of truth for 'which roles exist' — derived from the
    schema-validated `agents/profiles/*.yaml` sidecars (agents/profile.schema.json),
    not re-hardcoded here, so this module and the profile roster cannot
    silently drift apart. Deliberately NOT filtered against which roles have
    a matching .AGENTS.md — that check belongs to compose(), which fails
    loudly per-role instead of silently shrinking the roster."""
    yaml = _yaml()
    roles: list[str] = []
    for path in sorted(profiles_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "role" not in data:
            raise CompositionError(f"{path}: profile YAML missing required 'role' key")
        roles.append(data["role"])
    if not roles:
        raise CompositionError(f"no *.yaml profiles found under {profiles_dir}")
    return roles


def _slug_for(path: Path) -> str:
    name = path.name
    if not name.endswith(PROFILE_SUFFIX):
        raise CompositionError(f"{path}: filename does not end in '{PROFILE_SUFFIX}'")
    return name[: -len(PROFILE_SUFFIX)]


def _check_frontmatter(path: Path, text: str) -> dict:
    """Returns the parsed frontmatter dict, or raises CompositionError with a
    message that distinguishes 'missing entirely' from 'present but not
    first' from 'present but malformed' — the composition-integrity defect
    class this gate exists to catch loudly instead of silently."""
    yaml = _yaml()
    m = FRONTMATTER_AT_START_RE.match(text)
    if not m:
        if FRONTMATTER_ANYWHERE_RE.search(text):
            raise CompositionError(
                f"{path}: misplaced frontmatter — a '---' fence exists but is not "
                f"the first line of the file. A naive \\A-anchored frontmatter strip "
                f"(the common pattern for a deploy script that uploads these prompts "
                f"verbatim) would silently no-op here and ship whatever precedes the "
                f"fence — e.g. a leading comment — as raw prompt text. This repo has "
                f"no such deploy script today; this check guards the day one is added."
            )
        raise CompositionError(f"{path}: missing YAML frontmatter entirely")

    raw_fm = m.group(1)
    try:
        meta = yaml.safe_load(raw_fm)
    except yaml.YAMLError as e:
        raise CompositionError(f"{path}: frontmatter is not valid YAML: {e}") from e
    if not isinstance(meta, dict):
        raise CompositionError(f"{path}: frontmatter did not parse to a mapping")

    missing = REQUIRED_FRONTMATTER_KEYS - meta.keys()
    if missing:
        raise CompositionError(f"{path}: frontmatter missing required key(s): {sorted(missing)}")

    extra = meta.keys() - ALLOWED_FRONTMATTER_KEYS
    if extra:
        raise CompositionError(
            f"{path}: frontmatter has disallowed key(s) {sorted(extra)} — house "
            f"convention (agents/templates/AGENTS.template.md) limits frontmatter "
            f"to {sorted(ALLOWED_FRONTMATTER_KEYS)}"
        )

    role_value = str(meta["role"])
    if not ROLE_SLUG_RE.match(role_value):
        raise CompositionError(
            f"{path}: frontmatter 'role: {role_value!r}' is not a valid slug "
            f"(expected lowercase kebab-case, e.g. 'cost-guardian') — this is exactly "
            f"the shape an un-filled template placeholder like '<role-slug>' takes"
        )
    expected_slug = _slug_for(path)
    if role_value != expected_slug:
        raise CompositionError(
            f"{path}: frontmatter role '{role_value}' does not match filename slug "
            f"'{expected_slug}' — the routing key and the file it routes to have drifted"
        )

    return meta


def _check_no_unresolved_placeholders(path: Path, body: str) -> None:
    unresolved = sorted(set(PLACEHOLDER_RE.findall(body)))
    if unresolved:
        raise CompositionError(
            f"{path}: unresolved {{{{PLACEHOLDER}}}} token(s) in composed body: "
            f"{unresolved} — composition fails closed rather than shipping a "
            f"literal '{{{{...}}}}' token as prompt text"
        )


def compose(role: str, profiles_dir: Path = DEFAULT_PROFILES_DIR) -> dict:
    """Compose (validate + canonicalize) one role's deployed prompt. Returns
    a manifest dict with the composed bytes under key 'composed_text' (not
    written to disk here)."""
    path = profiles_dir / f"{role}{PROFILE_SUFFIX}"
    if not path.is_file():
        raise CompositionError(
            f"no prompt file for role '{role}' at {path} — this role has a YAML "
            f"contract but no system prompt (see agents/README.md: every role "
            f"ships both layers)"
        )
    raw = path.read_text(encoding="utf-8")

    meta = _check_frontmatter(path, raw)
    m = FRONTMATTER_AT_START_RE.match(raw)
    assert m is not None  # _check_frontmatter already proved this matches
    composed_text = raw[m.end():]
    _check_no_unresolved_placeholders(path, composed_text)

    composed_bytes = composed_text.encode("utf-8")
    raw_bytes = raw.encode("utf-8")

    try:
        source_path = str(path.relative_to(REPO_ROOT))
    except ValueError:
        # profiles_dir points outside the repo (e.g. a base-ref export to a
        # temp dir, or a self-test fixture) — an absolute path is still a
        # correct, if less tidy, identifier.
        source_path = str(path)

    return {
        "role": role,
        "source_path": source_path,
        "frontmatter": meta,
        "source": {"bytes": len(raw_bytes), "sha256": _sha256(raw_bytes)},
        "composed": {
            "bytes": len(composed_bytes),
            "sha256": _sha256(composed_bytes),
            "version": _sha256(composed_bytes)[:12],
        },
        "tokens": count_tokens(composed_text),
        "composed_text": composed_text,
    }


# ---------------------------------------------------------------------------
# Token estimation — no tokenizer here matches any specific deployed model;
# per agents/README.md, `model_tier` is an abstract label resolved by the
# model-router at runtime (PERSONA_TIERS_JSON maps roles to registered
# tiers), and the prompts are explicitly written to be model-agnostic. Every
# count below is a labeled ESTIMATE, not a measurement of real API usage.
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> dict:
    out = {
        "chars_per_4_estimate": {
            "count": round(len(text) / 4),
            "note": "ESTIMATE: len(text)/4 heuristic, model-agnostic, no real tokenizer used",
        }
    }
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        out["cl100k_base_estimate"] = {
            "count": len(enc.encode(text)),
            "note": "ESTIMATE: tiktoken cl100k_base (GPT-3.5/4 family tokenizer) — "
                    "used only as a widely-available reference tokenizer, not a "
                    "measurement of real deployed-model token usage",
        }
    except ImportError:
        out["cl100k_base_estimate"] = {
            "count": None,
            "note": "tiktoken not installed — skipped (pip install tiktoken to enable)",
        }
    return out


def compose_many(roles: list[str], profiles_dir: Path = DEFAULT_PROFILES_DIR) -> tuple[list[dict], list[str]]:
    manifests: list[dict] = []
    errors: list[str] = []
    for role in roles:
        try:
            manifests.append(compose(role, profiles_dir))
        except CompositionError as e:
            errors.append(f"{role}: {e}")
    return manifests, errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_compose(args: argparse.Namespace) -> int:
    try:
        m = compose(args.role, Path(args.profiles_dir))
    except CompositionError as e:
        print(f"COMPOSITION FAILED: {e}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(m["composed_text"], encoding="utf-8")
        print(f"wrote {m['composed']['bytes']} bytes to {args.out}")
    else:
        sys.stdout.write(m["composed_text"])
    if args.manifest:
        manifest_out = {k: v for k, v in m.items() if k != "composed_text"}
        Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
        Path(args.manifest).write_text(json.dumps(manifest_out, indent=2), encoding="utf-8")
        print(f"manifest written to {args.manifest}", file=sys.stderr)
    return 0


def cmd_compose_many(args: argparse.Namespace) -> int:
    profiles_dir = Path(args.profiles_dir)
    roles = args.roles.split(",") if args.roles else parse_roster(profiles_dir)
    manifests, errors = compose_many(roles, profiles_dir)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_list = []
    for m in manifests:
        composed_path = out_dir / f"{m['role']}.composed.md"
        composed_path.write_text(m["composed_text"], encoding="utf-8")
        entry = {k: v for k, v in m.items() if k != "composed_text"}
        entry["composed_path"] = str(composed_path)
        manifest_list.append(entry)

    (out_dir / "manifest.json").write_text(json.dumps(manifest_list, indent=2), encoding="utf-8")

    for m in manifest_list:
        print(f"  ok      {m['role']:15s} {m['composed']['bytes']:6d} bytes  "
              f"sha256={m['composed']['sha256'][:12]}")
    for e in errors:
        print(f"  FAILED  {e}", file=sys.stderr)

    print(f"\n{len(manifest_list)} composed, {len(errors)} failed loudly "
          f"-> {out_dir / 'manifest.json'}")
    return 1 if errors else 0


def cmd_report(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    print("| role | bytes | cl100k_base (ESTIMATE) | chars/4 (ESTIMATE) |")
    print("|---|---|---|---|")
    for m in manifest:
        t = m["tokens"]
        cl = t["cl100k_base_estimate"]["count"]
        c4 = t["chars_per_4_estimate"]["count"]
        print(f"| {m['role']} | {m['composed']['bytes']} | {cl if cl is not None else 'n/a'} | {c4} |")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_compose = sub.add_parser("compose", help="compose one role, print/write composed bytes")
    p_compose.add_argument("role", help="role slug, e.g. cost-guardian")
    p_compose.add_argument("--profiles-dir", default=str(DEFAULT_PROFILES_DIR))
    p_compose.add_argument("--out", help="write composed bytes to this path instead of stdout")
    p_compose.add_argument("--manifest", help="also write the manifest JSON to this path")
    p_compose.set_defaults(func=cmd_compose)

    p_many = sub.add_parser("compose-many", help="compose the roster (or --roles) into --out-dir")
    p_many.add_argument("--profiles-dir", default=str(DEFAULT_PROFILES_DIR))
    p_many.add_argument("--out-dir", required=True)
    p_many.add_argument("--roles", help="comma-separated role list; default = full roster from *.yaml")
    p_many.set_defaults(func=cmd_compose_many)

    p_report = sub.add_parser("report", help="print a Markdown byte/token table from a manifest.json")
    p_report.add_argument("--manifest", required=True)
    p_report.set_defaults(func=cmd_report)

    args = ap.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
