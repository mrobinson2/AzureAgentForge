"""Playbook-pack loader and template renderer for the AzureAgentForge Tenant Console.

This module is the pack's contract with the provisioning executor:
a vertical playbook pack is a
directory containing a ``pack.yaml`` manifest, agent AGENTS.md templates,
skill specs, seed pinned/durable memories, and a smoke-conversation fixture.
The executor loads the pack, collects wizard variables, renders everything,
and provisions the tenant from the rendered artifacts.

Design rules enforced here:

- Template variables are ``{{name}}`` — double curly braces around an
  identifier, nothing else. No mustache sections, no conditionals, no
  filters. Packs that need logic push it into the executor, not the
  template language.
- Rendering is strict: any placeholder without a matching variable raises
  ``ValueError`` listing *all* unresolved names, so a wizard gap surfaces
  as one actionable error instead of a drip of partial renders.
- ``pack.yaml`` must declare every variable used anywhere in the pack
  (agent templates, skills, seed memories, smoke fixture, name templates).
  ``validate_pack`` returns problems as plain strings so callers (console
  preflight, CLI, CI) can render them however they like.
- Seed memories and the smoke fixture are parsed as YAML *first* and
  rendered afterwards (string fields only), so wizard values containing
  quotes, colons, or newlines cannot corrupt the YAML.

Dependencies: stdlib + PyYAML only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

__all__ = [
    "AgentSpec",
    "Pack",
    "PLACEHOLDER_RE",
    "VALID_MEMORY_CLASSES",
    "example_variables",
    "find_placeholders",
    "load_pack",
    "load_seed_memories",
    "load_smoke_fixture",
    "render_agent",
    "render_structure",
    "render_template",
    "validate_pack",
]

#: ``{{identifier}}`` with optional inner whitespace. Single braces (JSON in
#: templates) never match.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: The governor's six memory classes.
VALID_MEMORY_CLASSES = frozenset(
    {"pinned", "durable_fact", "user_preference", "task_scoped", "ephemeral", "decaying"}
)

SEED_REQUIRED_FIELDS = (
    "seed_key",
    "content",
    "memory_class",
    "scope_kind",
    "source_type",
    "verification_state",
    "pin_request",
)

FIXTURE_REQUIRED_KEYS = (
    "title",
    "prompt",
    "timeout_seconds",
    "retry_on_no_wake_seconds",
    "max_retries",
    "expect",
)
FIXTURE_EXPECT_REQUIRED_KEYS = (
    "must_contain_any",
    "must_not_contain",
    "comment_min_chars",
    "status_in",
)


# --------------------------------------------------------------------------
# Rendering primitives
# --------------------------------------------------------------------------

def find_placeholders(text: str) -> List[str]:
    """Return the unique ``{{var}}`` names in *text*, in first-seen order."""
    return list(dict.fromkeys(PLACEHOLDER_RE.findall(text)))


def render_template(text: str, variables: Dict[str, Any]) -> str:
    """Substitute every ``{{var}}`` in *text* from *variables*.

    Raises ``ValueError`` naming ALL unresolved placeholders (sorted,
    deduplicated) if any placeholder has no matching variable. Values are
    stringified with ``str()``.
    """
    if not isinstance(text, str):
        raise TypeError(f"render_template expects str, got {type(text).__name__}")
    missing = sorted({name for name in PLACEHOLDER_RE.findall(text) if name not in variables})
    if missing:
        raise ValueError("unresolved template placeholders: " + ", ".join(missing))
    return PLACEHOLDER_RE.sub(lambda m: str(variables[m.group(1)]), text)


def _collect_placeholders(obj: Any, found: List[str]) -> None:
    if isinstance(obj, str):
        for name in PLACEHOLDER_RE.findall(obj):
            if name not in found:
                found.append(name)
    elif isinstance(obj, dict):
        for value in obj.values():
            _collect_placeholders(value, found)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _collect_placeholders(value, found)


def _substitute(obj: Any, variables: Dict[str, Any]) -> Any:
    if isinstance(obj, str):
        return PLACEHOLDER_RE.sub(lambda m: str(variables[m.group(1)]), obj)
    if isinstance(obj, dict):
        return {key: _substitute(value, variables) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_substitute(value, variables) for value in obj]
    return obj


def render_structure(obj: Any, variables: Dict[str, Any]) -> Any:
    """Render ``{{var}}`` placeholders in every string inside a parsed
    YAML/JSON structure (dicts, lists, strings; other scalars untouched).

    Like :func:`render_template`, raises ``ValueError`` listing ALL
    unresolved placeholders across the whole structure.
    """
    found: List[str] = []
    _collect_placeholders(obj, found)
    missing = sorted({name for name in found if name not in variables})
    if missing:
        raise ValueError("unresolved template placeholders: " + ", ".join(missing))
    return _substitute(obj, variables)


# --------------------------------------------------------------------------
# Pack model
# --------------------------------------------------------------------------

@dataclass
class AgentSpec:
    """One agent row from the manifest's ``agents:`` list."""

    role: str
    slug_prefix: str
    band: Any  # validated to int 1-4 by validate_pack
    template: str
    model: str
    toolsets: List[str]
    name_template: str
    reports_to: Optional[str] = None


@dataclass
class Pack:
    """A parsed (not necessarily valid) playbook pack.

    ``load_pack`` builds this permissively; run :func:`validate_pack` to get
    the list of problems (empty list == valid).
    """

    root: Path
    vertical: str
    display_name: str
    agents: List[AgentSpec]
    variables: Dict[str, Any]
    seed_memories_path: str
    smoke_fixture_path: str
    skills: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


def load_pack(path: Any) -> Pack:
    """Parse ``pack.yaml`` into a :class:`Pack`.

    *path* may be the pack directory or the ``pack.yaml`` file itself.
    Raises ``FileNotFoundError`` if the manifest is absent and ``ValueError``
    if it is not YAML or not a mapping. Content-level problems (missing
    template files, undeclared variables, bad bands, ...) are deliberately
    NOT raised here — collect them with :func:`validate_pack`.
    """
    p = Path(path)
    manifest = p if p.is_file() else p / "pack.yaml"
    if not manifest.is_file():
        raise FileNotFoundError(f"pack manifest not found: {manifest}")
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{manifest}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{manifest}: pack.yaml must parse to a mapping")

    agents: List[AgentSpec] = []
    raw_agents = data.get("agents") or []
    if isinstance(raw_agents, list):
        for entry in raw_agents:
            if not isinstance(entry, dict):
                continue
            toolsets = entry.get("toolsets") or []
            agents.append(
                AgentSpec(
                    role=str(entry.get("role") or ""),
                    slug_prefix=str(entry.get("slug_prefix") or ""),
                    band=entry.get("band"),
                    template=str(entry.get("template") or ""),
                    model=str(entry.get("model") or ""),
                    toolsets=list(toolsets) if isinstance(toolsets, list) else [toolsets],
                    name_template=str(entry.get("name_template") or ""),
                    reports_to=entry.get("reports_to"),
                )
            )

    variables = data.get("variables")
    skills = data.get("skills") or []

    return Pack(
        root=manifest.parent,
        vertical=str(data.get("vertical") or ""),
        display_name=str(data.get("display_name") or ""),
        agents=agents,
        variables=variables if isinstance(variables, dict) else {},
        seed_memories_path=str(data.get("seed_memories") or ""),
        smoke_fixture_path=str(data.get("smoke_fixture") or ""),
        skills=[str(s) for s in skills] if isinstance(skills, list) else [str(skills)],
        raw=data,
    )


def example_variables(pack: Pack) -> Dict[str, str]:
    """The ``example`` value of every declared variable, stringified.

    Used by the console preview, the smoke lane's dry-run, and the tests to
    prove the pack renders end-to-end without operator input.
    """
    out: Dict[str, str] = {}
    for name, decl in pack.variables.items():
        if isinstance(decl, dict) and "example" in decl:
            out[str(name)] = str(decl["example"])
    return out


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_pack(pack: Pack) -> List[str]:
    """Validate a pack; return a list of human-readable problems.

    Empty list == the pack is provisionable. Checks:

    - manifest basics (vertical, display_name, at least one agent)
    - each agent row is complete, band is 1-4, template file exists,
      ``reports_to`` references a declared role
    - every declared variable has description + example + required flag
    - seed-memories / smoke-fixture / skill files exist
    - every ``{{var}}`` used in any template, skill, seed file, fixture
      file, or ``name_template`` is declared in ``variables:``
    """
    problems: List[str] = []

    if not pack.vertical:
        problems.append("pack.yaml: 'vertical' is missing or empty")
    if not pack.display_name:
        problems.append("pack.yaml: 'display_name' is missing or empty")
    if not pack.agents:
        problems.append("pack.yaml: 'agents' must declare at least one agent")

    roles = [a.role for a in pack.agents]
    for role in sorted({r for r in roles if roles.count(r) > 1}):
        problems.append(f"pack.yaml: duplicate agent role '{role}'")
    known_roles = set(roles)

    for agent in pack.agents:
        label = f"pack.yaml agent '{agent.role or '?'}'"
        if not agent.role:
            problems.append(f"{label}: 'role' is missing or empty")
        if not agent.slug_prefix:
            problems.append(f"{label}: 'slug_prefix' is missing or empty")
        if not isinstance(agent.band, int) or isinstance(agent.band, bool) or agent.band not in (1, 2, 3, 4):
            problems.append(f"{label}: 'band' must be an integer 1-4 (got {agent.band!r})")
        if not agent.model:
            problems.append(f"{label}: 'model' is missing or empty")
        if not agent.toolsets or not all(isinstance(t, str) and t for t in agent.toolsets):
            problems.append(f"{label}: 'toolsets' must be a non-empty list of names")
        if not agent.name_template:
            problems.append(f"{label}: 'name_template' is missing or empty")
        if agent.reports_to is not None and agent.reports_to not in known_roles:
            problems.append(
                f"{label}: reports_to '{agent.reports_to}' is not a declared role"
            )
        if not agent.template:
            problems.append(f"{label}: 'template' is missing or empty")
        elif not (pack.root / agent.template).is_file():
            problems.append(f"{label}: template file not found: {agent.template}")

    for name, decl in pack.variables.items():
        if not isinstance(decl, dict):
            problems.append(f"pack.yaml variable '{name}': declaration must be a mapping")
            continue
        if not str(decl.get("description") or "").strip():
            problems.append(f"pack.yaml variable '{name}': missing 'description'")
        if "example" not in decl:
            problems.append(f"pack.yaml variable '{name}': missing 'example'")
        if not isinstance(decl.get("required"), bool):
            problems.append(
                f"pack.yaml variable '{name}': 'required' must be true or false"
            )

    for label, rel in (
        ("seed_memories", pack.seed_memories_path),
        ("smoke_fixture", pack.smoke_fixture_path),
    ):
        if not rel:
            problems.append(f"pack.yaml: '{label}' is missing")
        elif not (pack.root / rel).is_file():
            problems.append(f"pack.yaml: {label} file not found: {rel}")

    for rel in pack.skills:
        if not (pack.root / rel).is_file():
            problems.append(f"pack.yaml: skill file not found: {rel}")

    # Undeclared-variable scan: raw text of every pack file that gets
    # rendered, plus the manifest's own name_template strings.
    declared = set(pack.variables)
    scan_files: List[str] = [a.template for a in pack.agents if a.template]
    scan_files += list(pack.skills)
    scan_files += [rel for rel in (pack.seed_memories_path, pack.smoke_fixture_path) if rel]
    for rel in scan_files:
        f = pack.root / rel
        if not f.is_file():
            continue  # already reported above
        used = set(PLACEHOLDER_RE.findall(f.read_text(encoding="utf-8")))
        undeclared = sorted(used - declared)
        if undeclared:
            problems.append(f"{rel}: undeclared variable(s): {', '.join(undeclared)}")
    for agent in pack.agents:
        undeclared = sorted(set(PLACEHOLDER_RE.findall(agent.name_template or "")) - declared)
        if undeclared:
            problems.append(
                f"pack.yaml agent '{agent.role}': name_template uses undeclared "
                f"variable(s): {', '.join(undeclared)}"
            )

    return problems


# --------------------------------------------------------------------------
# Rendered artifacts
# --------------------------------------------------------------------------

def render_agent(pack: Pack, role: str, variables: Dict[str, Any]) -> str:
    """Render the AGENTS.md template for *role* with *variables*."""
    spec = next((a for a in pack.agents if a.role == role), None)
    if spec is None:
        raise KeyError(
            f"pack '{pack.vertical}' has no agent role {role!r} "
            f"(roles: {', '.join(a.role for a in pack.agents) or 'none'})"
        )
    template_path = pack.root / spec.template
    return render_template(template_path.read_text(encoding="utf-8"), variables)


def load_seed_memories(pack: Pack, variables: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse + render the pack's seed memories; validate the entries.

    Returns a list of dicts ready for the executor to upsert via the
    governor (idempotent on ``seed_key``). Raises ``ValueError`` on missing
    fields, invalid ``memory_class``, non-bool ``pin_request``, duplicate
    ``seed_key``, or unresolved placeholders.
    """
    path = pack.root / pack.seed_memories_path
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{pack.seed_memories_path}: must be a YAML list of seed entries")
    seeds = render_structure(raw, variables)

    problems: List[str] = []
    seen_keys: set = set()
    for i, seed in enumerate(seeds):
        where = f"{pack.seed_memories_path}[{i}]"
        if not isinstance(seed, dict):
            problems.append(f"{where}: entry must be a mapping")
            continue
        for fld in SEED_REQUIRED_FIELDS:
            if fld not in seed:
                problems.append(f"{where}: missing field '{fld}'")
        memory_class = seed.get("memory_class")
        if memory_class is not None and memory_class not in VALID_MEMORY_CLASSES:
            problems.append(
                f"{where}: invalid memory_class {memory_class!r} "
                f"(valid: {', '.join(sorted(VALID_MEMORY_CLASSES))})"
            )
        if "pin_request" in seed and not isinstance(seed["pin_request"], bool):
            problems.append(f"{where}: 'pin_request' must be true or false")
        if not str(seed.get("content") or "").strip():
            problems.append(f"{where}: 'content' is missing or empty")
        key = seed.get("seed_key")
        if key:
            if key in seen_keys:
                problems.append(f"{where}: duplicate seed_key '{key}'")
            seen_keys.add(key)
    if problems:
        raise ValueError("; ".join(problems))
    return seeds


def load_smoke_fixture(pack: Pack, variables: Dict[str, Any]) -> Dict[str, Any]:
    """Parse + render the smoke-conversation fixture; validate required keys.

    The returned dict is what the verification lane executes mechanically:
    open an issue with ``prompt``, wait/retry per the timing keys, assert
    the ``expect`` block against the agent's comment and issue status.
    """
    path = pack.root / pack.smoke_fixture_path
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{pack.smoke_fixture_path}: must be a YAML mapping")
    fixture = render_structure(raw, variables)

    problems = [f"missing key '{k}'" for k in FIXTURE_REQUIRED_KEYS if k not in fixture]
    expect = fixture.get("expect")
    if isinstance(expect, dict):
        problems += [
            f"expect: missing key '{k}'"
            for k in FIXTURE_EXPECT_REQUIRED_KEYS
            if k not in expect
        ]
        for list_key in ("must_contain_any", "must_not_contain", "status_in"):
            value = expect.get(list_key)
            if value is not None and (not isinstance(value, list) or not value):
                problems.append(f"expect: '{list_key}' must be a non-empty list")
    elif "expect" in fixture:
        problems.append("'expect' must be a mapping")
    for int_key in ("timeout_seconds", "retry_on_no_wake_seconds", "max_retries"):
        value = fixture.get(int_key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            problems.append(f"'{int_key}' must be an integer")
    if problems:
        raise ValueError(f"{pack.smoke_fixture_path}: " + "; ".join(problems))
    return fixture
