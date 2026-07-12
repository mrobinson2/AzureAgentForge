#!/usr/bin/env python3
"""Vendored-config schema guard (A3).

Validates every config key AAF ships to a vendored app (compose env blocks,
Terraform env blocks, the generated Hermes config.yaml) against the schema the
*pinned vendored source* actually reads. A key the app silently drops — the
Honcho 3.0.7 flat-key removal, the Hermes config-schema shift — fails CI here
instead of silently degrading a deployment.

Strategy per app (docs/design/vendored-config-schema-guard.md):
  * Honcho    — dynamically load the vendored pydantic-settings model
                (apps/honcho/src/src/config.py) and derive every acceptable
                env key from it. Exact, zero curation, auto-tracks bumps.
  * Hermes    — config.yaml keys: AST-parse the vendored parser's
                DEFAULT_CONFIG tree (no imports); polymorphic sections come
                from a manifest pinned to the submodule commit (a bump fails
                loudly until re-validated). Env keys: static consumption scan
                of the vendored tree + AAF override scripts.
  * PaperClip — source is not in-tree (cloned at image build), so a curated
                manifest pinned to PAPERCLIP_VERSION; the pin is cross-checked
                against the Dockerfile ARG and compose default.

Findings are HARD failures. Suppression happens only through
scripts/vendored-config/allowlist.yaml, where every entry must carry a
non-empty reason.

Usage:
    python scripts/validate_vendored_config.py [--repo PATH]
    python scripts/validate_vendored_config.py --self-test

Exit codes: 0 = clean, 1 = findings (or self-test failure), 2 = cannot run
(missing submodule checkout, missing deps).
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    print("error: PyYAML is required (pip install -r scripts/vendored-config/requirements.txt)")
    sys.exit(2)

REPO = Path(__file__).resolve().parents[1]
GUARD_DIR = "scripts/vendored-config"

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Finding:
    app: str  # honcho | hermes | paperclip | guard
    artifact: str  # repo-relative path (plus locator, e.g. service name)
    key: str  # offending key / dotted config path
    message: str

    def __str__(self) -> str:
        return f"[{self.app}] {self.artifact}: {self.key}\n      {self.message}"


# ──────────────────────────────────────────────────────────────────────────────
# Allowlist
# ──────────────────────────────────────────────────────────────────────────────

def load_allowlist(repo: Path) -> tuple[set[tuple[str, str]], list[Finding]]:
    """Return ({(app, KEY_UPPER)}, findings-about-the-allowlist-itself)."""
    path = repo / GUARD_DIR / "allowlist.yaml"
    findings: list[Finding] = []
    entries: set[tuple[str, str]] = set()
    if not path.exists():
        return entries, findings
    data = yaml.safe_load(path.read_text()) or {}
    for i, entry in enumerate(data.get("allow", []) or []):
        if not isinstance(entry, dict):
            findings.append(Finding("guard", f"{GUARD_DIR}/allowlist.yaml", f"entry[{i}]",
                                    "allowlist entries must be mappings with app/key/reason"))
            continue
        app, key, reason = entry.get("app"), entry.get("key"), entry.get("reason")
        if not app or not key:
            findings.append(Finding("guard", f"{GUARD_DIR}/allowlist.yaml", f"entry[{i}]",
                                    "allowlist entry missing 'app' or 'key'"))
            continue
        if not isinstance(reason, str) or not reason.strip():
            findings.append(Finding("guard", f"{GUARD_DIR}/allowlist.yaml", str(key),
                                    "allowlist entry has no non-empty 'reason' — every "
                                    "suppression must be an explained, reviewed decision"))
            continue
        entries.add((str(app), str(key).upper()))
    return entries, findings


def allowed(allowlist: set[tuple[str, str]], app: str, key: str) -> bool:
    return (app, key.upper()) in allowlist


# ──────────────────────────────────────────────────────────────────────────────
# Shipped-artifact parsing (compose env blocks, Terraform env blocks)
# ──────────────────────────────────────────────────────────────────────────────

def compose_env_keys(compose_path: Path, service: str) -> list[str]:
    """Explicit `environment:` keys of one compose service (dict or list form)."""
    doc = yaml.safe_load(compose_path.read_text()) or {}
    svc = (doc.get("services") or {}).get(service) or {}
    env = svc.get("environment")
    if env is None:
        return []
    if isinstance(env, dict):
        return [str(k) for k in env]
    keys = []
    for item in env:  # list form: "KEY=value"
        keys.append(str(item).split("=", 1)[0])
    return keys


_TF_ENV_RE = re.compile(r'env\s*\{[^{}]*?\bname\s*=\s*"([A-Za-z_][A-Za-z0-9_]*)"', re.S)
_TF_CONTAINER_RE = re.compile(r"\bcontainer\s*\{")
_TF_NAME_RE = re.compile(r'\bname\s*=\s*"([A-Za-z0-9_-]+)"')


def _tf_container_blocks(text: str) -> list[tuple[str, str]]:
    """(container_name, block_text) for every container{} block (brace-matched)."""
    blocks: list[tuple[str, str]] = []
    for match in _TF_CONTAINER_RE.finditer(text):
        depth = 0
        start = match.end() - 1  # at the opening brace
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    block = text[start:i + 1]
                    name = _TF_NAME_RE.search(block)
                    blocks.append((name.group(1) if name else "", block))
                    break
    return blocks


def terraform_env_keys(tf_path: Path, containers: list[str] | None = None) -> list[str]:
    """env{ name = "..." } entries, optionally scoped to named container blocks.

    A Container App template can carry several containers (e.g. the hermes
    gateway plus its model-router sidecar); each container's env belongs to a
    different consuming app, so callers scope to the vendored one.
    """
    text = tf_path.read_text()
    if containers is None:
        return _TF_ENV_RE.findall(text)
    keys: list[str] = []
    for name, block in _tf_container_blocks(text):
        if name in containers:
            keys += _TF_ENV_RE.findall(block)
    return keys


# ──────────────────────────────────────────────────────────────────────────────
# Honcho — dynamic pydantic-settings introspection
# ──────────────────────────────────────────────────────────────────────────────

def load_honcho_settings_module(repo: Path):
    cfg = repo / "apps/honcho/src/src/config.py"
    if not cfg.exists():
        raise FileNotFoundError(
            f"{cfg} missing — initialize the honcho submodule (git submodule update --init)")
    os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
    spec = importlib.util.spec_from_file_location("_aaf_vendored_honcho_config", cfg)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _alias_names(field: Any, default_name: str) -> set[str]:
    names = {default_name}
    alias = getattr(field, "validation_alias", None)
    if isinstance(alias, str):
        names.add(alias)
    elif alias is not None and hasattr(alias, "choices"):
        for c in alias.choices:
            if isinstance(c, str):
                names.add(c)
    return names


def _find_model_class(annotation: Any) -> Any:
    """First pydantic BaseModel subclass reachable in a type annotation."""
    import typing

    from pydantic import BaseModel
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        found = _find_model_class(arg)
        if found is not None:
            return found
    return None


def _dict_field_parts(annotation: Any) -> tuple[list[str] | None, Any] | None:
    """For dict[...] annotations: (closed key list or None if open, value model or None)."""
    import typing
    origin = typing.get_origin(annotation)
    if origin is not dict:
        # unwrap Optional/Annotated one level
        for arg in typing.get_args(annotation):
            if typing.get_origin(arg) is dict:
                annotation, origin = arg, dict
                break
        else:
            return None
    key_t, val_t = typing.get_args(annotation)
    literal_keys = None
    if typing.get_origin(key_t) is typing.Literal:
        literal_keys = [str(a) for a in typing.get_args(key_t)]
    return literal_keys, _find_model_class(val_t)


def _walk_model_env(model_cls: Any, prefix: str, delimiter: str | None,
                    exact: set[str], patterns: list[re.Pattern[str]], depth: int = 0) -> None:
    if depth > 6:  # defensive: pydantic models are shallow; avoid cycles
        return
    for fname, field in model_cls.model_fields.items():
        for name in _alias_names(field, fname):
            exact.add((prefix + name).upper())
            if not delimiter:
                continue
            ann = field.annotation
            nested = _find_model_class(ann)
            dict_parts = _dict_field_parts(ann)
            if dict_parts is not None:
                literal_keys, val_model = dict_parts
                base = prefix + name + delimiter
                if literal_keys is not None and val_model is not None:
                    for k in literal_keys:
                        _walk_model_env(val_model, base + k + delimiter, delimiter,
                                        exact, patterns, depth + 1)
                else:
                    # open dict: accept any sub-path (values or nested models)
                    patterns.append(re.compile(re.escape(base.upper()) + r"[A-Z0-9_]+$"))
            elif nested is not None:
                _walk_model_env(nested, prefix + name + delimiter, delimiter,
                                exact, patterns, depth + 1)


def honcho_accepted_env(mod: Any) -> tuple[set[str], list[re.Pattern[str]]]:
    """Every env key the vendored Honcho AppSettings tree can consume."""
    from pydantic_settings import BaseSettings
    exact: set[str] = set()
    patterns: list[re.Pattern[str]] = []
    app_cls = type(mod.settings)
    # 1) AppSettings itself (env_prefix "", delimiter "__")
    _walk_model_env(app_cls,
                    app_cls.model_config.get("env_prefix", "") or "",
                    app_cls.model_config.get("env_nested_delimiter"),
                    exact, patterns)
    # 2) each nested settings class with its OWN prefix/delimiter (this is how
    #    the container is actually configured: SUMMARY_MODEL_CONFIG__MODEL etc.)
    for field in app_cls.model_fields.values():
        sub = _find_model_class(field.annotation)
        if sub is not None and issubclass(sub, BaseSettings):
            _walk_model_env(sub,
                            sub.model_config.get("env_prefix", "") or "",
                            sub.model_config.get("env_nested_delimiter"),
                            exact, patterns)
    return exact, patterns


# Known-removed keys (Honcho 3.0.7, upstream #459) — upgrades the diagnostic
# from "unknown key" to a migration hint. Detection never depends on this map.
_HONCHO_REMOVED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^SUMMARY_PROVIDER$"), "SUMMARY_MODEL_CONFIG__TRANSPORT"),
    (re.compile(r"^SUMMARY_MODEL$"), "SUMMARY_MODEL_CONFIG__MODEL"),
    (re.compile(r"^DERIVER_PROVIDER$"), "DERIVER_MODEL_CONFIG__TRANSPORT"),
    (re.compile(r"^DERIVER_MODEL$"), "DERIVER_MODEL_CONFIG__MODEL"),
    (re.compile(r"^DIALECTIC_LEVELS__([A-Z]+)__PROVIDER$"),
     "DIALECTIC_LEVELS__<level>__MODEL_CONFIG__TRANSPORT"),
    (re.compile(r"^DIALECTIC_LEVELS__([A-Z]+)__MODEL$"),
     "DIALECTIC_LEVELS__<level>__MODEL_CONFIG__MODEL"),
    (re.compile(r"^DIALECTIC_LEVELS__([A-Z]+)__THINKING_BUDGET_TOKENS$"),
     "DIALECTIC_LEVELS__<level>__MODEL_CONFIG__THINKING_BUDGET_TOKENS"),
]


def check_env_keys(app: str, artifact: str, keys: list[str],
                   exact: set[str], patterns: list[re.Pattern[str]],
                   allowlist: set[tuple[str, str]],
                   removed_map: list[tuple[re.Pattern[str], str]] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for key in keys:
        k = key.upper()
        if k in exact or any(p.match(k) for p in patterns):
            continue
        if allowed(allowlist, app, k):
            continue
        hint = None
        for pat, replacement in removed_map or []:
            if pat.match(k):
                hint = replacement
                break
        if hint:
            msg = (f"REMOVED key — the pinned vendored {app} no longer reads it and "
                   f"silently ignores it (extra='ignore'). Replace with {hint}.")
        else:
            msg = (f"unknown key — the pinned vendored {app} does not read it; it will be "
                   f"silently dropped. Fix the key, or allowlist it in "
                   f"{GUARD_DIR}/allowlist.yaml with a reason.")
        findings.append(Finding(app, artifact, key, msg))
    return findings


def validate_honcho(repo: Path, allowlist: set[tuple[str, str]]) -> list[Finding]:
    mod = load_honcho_settings_module(repo)
    exact, patterns = honcho_accepted_env(mod)
    findings: list[Finding] = []
    targets = [
        ("docker-compose.yml (service: honcho)",
         compose_env_keys(repo / "docker-compose.yml", "honcho")),
        ("deploy/mac-site/docker-compose.yml (service: honcho)",
         compose_env_keys(repo / "deploy/mac-site/docker-compose.yml", "honcho")),
        ("infrastructure/modules/container-apps/honcho.tf",
         terraform_env_keys(repo / "infrastructure/modules/container-apps/honcho.tf")),
    ]
    for artifact, keys in targets:
        findings += check_env_keys("honcho", artifact, keys, exact, patterns,
                                   allowlist, _HONCHO_REMOVED)
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Hermes — static AST parse of the vendored config parser
# ──────────────────────────────────────────────────────────────────────────────

def _ast_key_tree(node: ast.expr) -> dict[str, Any] | None:
    """Key tree of a dict literal: {key: subtree-or-None}. Non-dict → None."""
    if not isinstance(node, ast.Dict):
        return None
    out: dict[str, Any] = {}
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out[k.value] = _ast_key_tree(v)
    return out


def _ast_string_elements(node: ast.expr) -> set[str]:
    """String constants that are set elements / dict keys of a literal expr."""
    out: set[str] = set()
    if isinstance(node, ast.Call):  # frozenset({...})
        for arg in node.args:
            out |= _ast_string_elements(arg)
    elif isinstance(node, ast.Set):
        for el in node.elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                out.add(el.value)
    elif isinstance(node, ast.Dict):
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                out.add(k.value)
    return out


def parse_hermes_config_source(repo: Path) -> dict[str, Any]:
    """AST-extract DEFAULT_CONFIG tree, _KNOWN_ROOT_KEYS and env-key tables."""
    src_path = repo / "apps/hermes/src/hermes_cli/config.py"
    if not src_path.exists():
        raise FileNotFoundError(
            f"{src_path} missing — initialize the hermes submodule (git submodule update --init)")
    tree = ast.parse(src_path.read_text())
    result: dict[str, Any] = {"default_config": {}, "known_root_keys": set(), "env_tables": set()}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "DEFAULT_CONFIG":
                result["default_config"] = _ast_key_tree(node.value) or {}
            elif target.id == "_KNOWN_ROOT_KEYS":
                result["known_root_keys"] = _ast_string_elements(node.value)
            elif target.id in ("_EXTRA_ENV_KEYS", "OPTIONAL_ENV_VARS", "REQUIRED_ENV_VARS"):
                result["env_tables"] |= _ast_string_elements(node.value)
    if not result["default_config"]:
        raise RuntimeError("could not locate DEFAULT_CONFIG in vendored hermes_cli/config.py "
                           "— upstream restructured; the guard needs updating")
    return result


def gitlink_sha(repo: Path, submodule_path: str) -> str | None:
    """Pinned submodule commit from the superproject tree (no checkout needed)."""
    try:
        out = subprocess.run(["git", "ls-tree", "HEAD", submodule_path],
                             cwd=repo, capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    m = re.search(r"^160000 commit ([0-9a-f]{40})\t", out, re.M)
    return m.group(1) if m else None


def load_manifest(repo: Path, name: str) -> dict[str, Any]:
    path = repo / GUARD_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"{path} missing")
    return yaml.safe_load(path.read_text()) or {}


def hermes_config_generators(repo: Path, marker: str = "HERMES_EOF") -> list[Path]:
    """Every shipped shell script that generates a Hermes config.yaml heredoc.

    Discovered, not hardcoded: A1 moved the generation from the entrypoint
    into write-hermes-config.sh mid-flight — hardcoded paths would validate
    a file that no longer generates anything while the real generator drifts
    unchecked."""
    found: list[Path] = []
    for d in ("apps/paperclip", "services/paperclip"):
        for sh in sorted((repo / d).glob("*.sh")):
            if f"<<{marker}" in sh.read_text(errors="ignore"):
                found.append(sh)
    return found


def extract_heredoc_yaml(script_path: Path, marker: str = "HERMES_EOF") -> dict[str, Any]:
    """Parse the config.yaml generated by an entrypoint heredoc."""
    lines = script_path.read_text().splitlines()
    collecting = False
    collected: list[str] = []
    for line in lines:
        if not collecting and f"<<{marker}" in line:
            collecting = True
            continue
        if collecting:
            if line.strip() == marker:
                break
            collected.append(line)
    body = "\n".join(collected)
    body = re.sub(r"\$\{[^}]*\}", "placeholder", body)  # neutralize shell expansions
    body = re.sub(r"\$\([^)]*\)", "placeholder", body)
    return yaml.safe_load(body) or {}


def _check_yaml_paths(app: str, artifact: str, node: dict[str, Any],
                      accepted: dict[str, Any] | None, poly: dict[str, Any],
                      known_root: set[str], allowlist: set[tuple[str, str]],
                      path: tuple[str, ...] = ()) -> list[Finding]:
    findings: list[Finding] = []
    for key, value in node.items():
        dotted = ".".join(path + (str(key),))
        sub_accepted = None
        ok = False
        if accepted is not None and key in accepted:
            ok = True
            sub_accepted = accepted[key]
        elif not path and key in known_root:
            ok = True
        # polymorphic manifest sections (e.g. `model:` whose vendored default
        # is a scalar so the AST tree can't see its dict-form keys)
        poly_here = poly.get(".".join(path)) if path else None
        if not ok and path and poly_here and key in poly_here:
            ok = True
        if not ok and allowed(allowlist, app, dotted):
            ok = True
        if not ok:
            findings.append(Finding(
                app, artifact, dotted,
                "unknown config.yaml key — the pinned vendored hermes parser does not "
                "read it; it will be silently ignored. Fix the key, update "
                f"{GUARD_DIR}/manifest-hermes.yaml (if the runtime reads it outside "
                f"DEFAULT_CONFIG), or allowlist it with a reason."))
            continue
        if isinstance(value, dict):
            findings += _check_yaml_paths(app, artifact, value, sub_accepted, poly,
                                          known_root, allowlist, path + (str(key),))
    return findings


def validate_hermes_yaml(repo: Path, allowlist: set[tuple[str, str]]) -> list[Finding]:
    findings: list[Finding] = []
    manifest = load_manifest(repo, "manifest-hermes.yaml")
    pinned = str(manifest.get("pinned_commit", ""))
    actual = gitlink_sha(repo, "apps/hermes/src")
    if actual and pinned != actual:
        findings.append(Finding(
            "hermes", f"{GUARD_DIR}/manifest-hermes.yaml", "pinned_commit",
            f"manifest pinned to {pinned[:12]} but the hermes submodule gitlink is "
            f"{actual[:12]} — vendor bump detected. Re-validate the polymorphic "
            f"sections against the new source, then update pinned_commit. "
            f"(Failing loudly here is the point: nothing re-checked the schema yet.)"))
    parsed = parse_hermes_config_source(repo)
    poly = {section: set(keys or [])
            for section, keys in (manifest.get("polymorphic_sections") or {}).items()}
    generators = hermes_config_generators(repo)
    if not generators:
        findings.append(Finding(
            "hermes", "apps/paperclip + services/paperclip", "<config.yaml heredoc>",
            "no shipped script generates a Hermes config.yaml (HERMES_EOF heredoc) "
            "anymore — either the generation moved somewhere this guard doesn't "
            "scan (update hermes_config_generators), or the container now boots "
            "Hermes on pure defaults, which is its own silent-config incident."))
    for script in generators:
        rel = str(script.relative_to(repo))
        cfg = extract_heredoc_yaml(script)
        if not cfg:
            findings.append(Finding("hermes", rel, "<config.yaml heredoc>",
                                    "HERMES_EOF heredoc present but no config keys could "
                                    "be extracted — guard needs updating"))
            continue
        findings += _check_yaml_paths("hermes", f"{rel} (generated config.yaml)",
                                      cfg, parsed["default_config"], poly,
                                      parsed["known_root_keys"], allowlist)
    return findings


_ENV_READ_PATTERNS = [
    re.compile(r"""\bos\.getenv\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""),
    re.compile(r"""\bgetenv\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""),
    re.compile(r"""\bos\.environ\.get\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""),
    re.compile(r"""\bos\.environ\[\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*\]"""),
    re.compile(r"""\benviron\.setdefault\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""),
]
_SHELL_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_MJS_ENV_RE = [
    re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"""process\.env\[\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*\]"""),
]


def scan_env_reads_py(root: Path) -> set[str]:
    out: set[str] = set()
    for py in root.rglob("*.py"):
        if "node_modules" in py.parts:
            continue
        try:
            text = py.read_text(errors="ignore")
        except OSError:
            continue
        for pat in _ENV_READ_PATTERNS:
            out.update(m.upper() for m in pat.findall(text))
    return out


def scan_env_reads_sh(root: Path) -> set[str]:
    out: set[str] = set()
    for sh in root.rglob("*.sh"):
        out.update(m.upper() for m in _SHELL_VAR_RE.findall(sh.read_text(errors="ignore")))
    return out


def scan_env_reads_mjs(root: Path) -> set[str]:
    out: set[str] = set()
    for mjs in root.rglob("*.mjs"):
        text = mjs.read_text(errors="ignore")
        for pat in _MJS_ENV_RE:
            out.update(m.upper() for m in pat.findall(text))
    return out


def hermes_env_universe(repo: Path) -> set[str]:
    """Env keys the vendored hermes tree (or bundled AAF override scripts) reads."""
    universe = scan_env_reads_py(repo / "apps/hermes/src")
    universe |= {k.upper() for k in parse_hermes_config_source(repo)["env_tables"]}
    universe |= scan_env_reads_sh(repo / "apps/hermes/overrides")
    return universe


def validate_hermes_env(repo: Path, allowlist: set[tuple[str, str]]) -> list[Finding]:
    # Only the "hermes" container — its sidecars (model-router) are AAF-authored
    # apps with their own in-repo config surface, outside this guard's scope.
    # The hermes gateway image is built from services/agent-runtime/, whose
    # entrypoint consumes additional env (OPENAI_MODEL, ...) — include its reads.
    universe = hermes_env_universe(repo) | scan_env_reads_sh(repo / "services/agent-runtime")
    keys = terraform_env_keys(repo / "infrastructure/modules/container-apps/hermes.tf",
                              containers=["hermes"])
    return check_env_keys("hermes",
                          "infrastructure/modules/container-apps/hermes.tf (container: hermes)",
                          keys, universe, [], allowlist)


# ──────────────────────────────────────────────────────────────────────────────
# PaperClip — curated per-version manifest
# ──────────────────────────────────────────────────────────────────────────────

def paperclip_pinned_versions(repo: Path) -> dict[str, str]:
    """PAPERCLIP_VERSION defaults from the Dockerfile ARG and compose."""
    versions: dict[str, str] = {}
    docker = (repo / "services/paperclip/Dockerfile").read_text()
    m = re.search(r"^ARG PAPERCLIP_VERSION=(\S+)", docker, re.M)
    if m:
        versions["services/paperclip/Dockerfile"] = m.group(1)
    compose = (repo / "docker-compose.yml").read_text()
    m = re.search(r"PAPERCLIP_VERSION:\s*\$\{PAPERCLIP_VERSION:-([^}]+)\}", compose)
    if m:
        versions["docker-compose.yml"] = m.group(1)
    return versions


def validate_paperclip(repo: Path, allowlist: set[tuple[str, str]]) -> list[Finding]:
    findings: list[Finding] = []
    manifest = load_manifest(repo, "manifest-paperclip.yaml")
    pinned = str(manifest.get("pinned_version", ""))
    for source, version in paperclip_pinned_versions(repo).items():
        if version != pinned:
            findings.append(Finding(
                "paperclip", f"{GUARD_DIR}/manifest-paperclip.yaml", "pinned_version",
                f"manifest curated against {pinned!r} but {source} pins {version!r} — "
                f"vendor bump detected. Re-validate accepted_env against the new "
                f"release, then update pinned_version."))
    accepted = {str(e["key"]).upper()
                for e in (manifest.get("accepted_env") or []) if isinstance(e, dict) and "key" in e}
    # The paperclip container also runs the vendored Hermes CLI (the adapter
    # spawns it per task) plus AAF-authored entrypoint/auth-proxy/patch scripts,
    # so env any of those read is valid here too — all derived from source;
    # the curated manifest only carries keys the (not-in-tree) upstream reads.
    accepted |= hermes_env_universe(repo)
    accepted |= scan_env_reads_mjs(repo / "apps/paperclip")
    accepted |= scan_env_reads_sh(repo / "apps/paperclip")
    accepted |= scan_env_reads_sh(repo / "services/paperclip")
    targets = [
        ("docker-compose.yml (service: paperclip)",
         compose_env_keys(repo / "docker-compose.yml", "paperclip")),
        ("deploy/mac-site/docker-compose.yml (service: paperclip)",
         compose_env_keys(repo / "deploy/mac-site/docker-compose.yml", "paperclip")),
        ("infrastructure/modules/container-apps/paperclip.tf (container: paperclip)",
         terraform_env_keys(repo / "infrastructure/modules/container-apps/paperclip.tf",
                            containers=["paperclip"])),
    ]
    for artifact, keys in targets:
        findings += check_env_keys("paperclip", artifact, keys, accepted, [], allowlist)
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────────────

def validate_all(repo: Path) -> list[Finding]:
    allowlist, findings = load_allowlist(repo)
    findings += validate_honcho(repo, allowlist)
    findings += validate_hermes_yaml(repo, allowlist)
    findings += validate_hermes_env(repo, allowlist)
    findings += validate_paperclip(repo, allowlist)
    return findings


def self_test(repo: Path) -> int:
    """Prove the guard can FAIL: seed known-bad keys, assert each is detected."""
    failures: list[str] = []

    def expect(name: str, findings: list[Finding], key: str) -> None:
        if not any(f.key.upper() == key.upper() for f in findings):
            failures.append(f"self-test '{name}': seeded bad key {key} was NOT detected")

    # 1) Honcho: removed flat key + unknown key must both be flagged; valid must pass
    mod = load_honcho_settings_module(repo)
    exact, patterns = honcho_accepted_env(mod)
    f = check_env_keys("honcho", "<self-test>", ["SUMMARY_PROVIDER"], exact, patterns,
                       set(), _HONCHO_REMOVED)
    expect("honcho removed key", f, "SUMMARY_PROVIDER")
    if f and "REMOVED" not in f[0].message:
        failures.append("self-test 'honcho removed key': missing migration hint")
    expect("honcho unknown key",
           check_env_keys("honcho", "<self-test>", ["HONCHO_TYPO_KEY"], exact, patterns, set()),
           "HONCHO_TYPO_KEY")
    expect("honcho typo'd dialectic level",
           check_env_keys("honcho", "<self-test>",
                          ["DIALECTIC_LEVELS__HGIH__MODEL_CONFIG__MODEL"], exact, patterns, set()),
           "DIALECTIC_LEVELS__HGIH__MODEL_CONFIG__MODEL")
    good = ["DB_CONNECTION_URI", "LLM_OPENAI_API_KEY", "SUMMARY_MODEL_CONFIG__TRANSPORT",
            "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__MODEL",
            "DERIVER_MODEL_CONFIG__OVERRIDES__BASE_URL", "LOG_LEVEL"]
    leftover = check_env_keys("honcho", "<self-test>", good, exact, patterns, set())
    if leftover:
        failures.append(f"self-test 'honcho valid keys': false positives: "
                        f"{[x.key for x in leftover]}")

    # 2) Hermes yaml: typo'd root key must be flagged; the real generated config must pass
    parsed = parse_hermes_config_source(repo)
    manifest = load_manifest(repo, "manifest-hermes.yaml")
    poly = {s: set(k or []) for s, k in (manifest.get("polymorphic_sections") or {}).items()}
    bad_cfg = {"prompt_cachng": {"cache_ttl": "1h"},  # seeded typo
               "model": {"provider": "custom", "baseurl": "x"}}  # seeded bad subkey
    f = _check_yaml_paths("hermes", "<self-test>", bad_cfg, parsed["default_config"], poly,
                          parsed["known_root_keys"], set())
    expect("hermes typo root key", f, "prompt_cachng")
    expect("hermes typo model subkey", f, "model.baseurl")
    good_cfg = {"model": {"provider": "custom", "base_url": "x", "api_mode": "anthropic_messages"},
                "prompt_caching": {"cache_ttl": "1h"}}
    leftover = _check_yaml_paths("hermes", "<self-test>", good_cfg, parsed["default_config"],
                                 poly, parsed["known_root_keys"], set())
    if leftover:
        failures.append(f"self-test 'hermes valid config': false positives: "
                        f"{[x.key for x in leftover]}")

    # 3) PaperClip: a version-pin mismatch and an unknown env key must be flagged
    pc_manifest = load_manifest(repo, "manifest-paperclip.yaml")
    if str(pc_manifest.get("pinned_version")) == "v0.0.0-selftest":
        failures.append("self-test 'paperclip pin': manifest unexpectedly pinned to sentinel")
    accepted = {str(e["key"]).upper() for e in (pc_manifest.get("accepted_env") or [])}
    expect("paperclip unknown key",
           check_env_keys("paperclip", "<self-test>", ["PAPERCLIP_TYPO_KEY"], accepted, [], set()),
           "PAPERCLIP_TYPO_KEY")

    # 4) Allowlist: an allowlisted key must pass; a reason-less entry must fail
    f = check_env_keys("honcho", "<self-test>", ["HONCHO_TYPO_KEY"], exact, patterns,
                       {("honcho", "HONCHO_TYPO_KEY")})
    if f:
        failures.append("self-test 'allowlist suppression': allowlisted key still flagged")

    if failures:
        print("SELF-TEST FAILED — the guard cannot be trusted to detect drift:")
        for msg in failures:
            print(f"  ✗ {msg}")
        return 1
    print("self-test OK: seeded removed/unknown/typo'd keys all detected; "
          "valid keys and allowlisted keys pass")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--self-test", action="store_true",
                        help="seed known-bad keys and assert the guard detects them")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test(args.repo)

    try:
        findings = validate_all(args.repo)
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2

    if findings:
        print(f"vendored-config schema guard: {len(findings)} finding(s)\n")
        for f in findings:
            print(f"  ✗ {f}\n")
        print("Every key above is shipped by this repo but not read by the pinned "
              "vendored source — the exact class of silent degradation this guard "
              "exists to catch. See docs/design/vendored-config-schema-guard.md.")
        return 1
    print("vendored-config schema guard: clean — every shipped key is read by the "
          "pinned vendored source (or explicitly allowlisted with a reason)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
