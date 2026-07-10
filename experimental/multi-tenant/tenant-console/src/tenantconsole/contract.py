"""Tenant contract — the one row of config a tenant IS.

A tenant contract is a single YAML document (`tenant: {...}`) that the
provisioning executor turns into a PaperClip company + agents + seeded
workspace memory + channel bindings. Everything else is *derived* from it:

- ``workspace`` = ``tenant-<slug>`` (the governor memory partition).
- The four "structural" render variables (``tenant_slug``, ``tenant_display_name``,
  ``vertical``, ``workspace``) are injected into the wizard variables before any
  template renders, so a contract only has to carry the pack's *collected*
  variables (service area, pricing bands, ...).

Validation is strict and total: :func:`load_contract` collects EVERY problem and
raises a single :class:`ContractError` listing all of them, so a malformed
contract surfaces as one actionable error rather than a drip. The executor never
sees an invalid contract.

Dependencies: stdlib + PyYAML only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

__all__ = [
    "SLUG_RE",
    "VERTICAL_RE",
    "ChannelBinding",
    "ContractError",
    "TenantContract",
    "load_contract",
    "repo_playbooks_root",
]

#: DNS-safe, immutable tenant slug: lowercase alnum, internal hyphens, 3-40 chars.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38})[a-z0-9]$")

#: A vertical names a playbook pack DIRECTORY under ``playbooks/``. It is joined
#: onto a filesystem path, so it is validated as strictly as the slug (aaf-0012):
#: lowercase alnum + internal hyphens, 3-40 chars — no dots, slashes, or ``..``
#: that could traverse out of the playbooks root.
VERTICAL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38})[a-z0-9]$")

#: The render variables the contract loader derives and injects itself; a
#: contract's ``variables:`` block must NOT need to supply these.
DERIVED_VARIABLES = ("tenant_slug", "tenant_display_name", "vertical", "workspace")


class ContractError(ValueError):
    """A tenant contract failed to load or validate. Message lists all problems."""


def repo_playbooks_root() -> Path:
    """Absolute ``<tenant-console>/playbooks`` for this checkout (default pack location)."""
    # contract.py -> tenantconsole -> src -> tenant-console
    return Path(__file__).resolve().parents[2] / "playbooks"


@dataclass
class ChannelBinding:
    """One intake/notification surface to bind for the tenant."""

    kind: str
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TenantContract:
    """A validated tenant provisioning contract."""

    slug: str
    display_name: str
    vertical: str
    daily_usd: float
    per_run_usd: float
    variables: Dict[str, Any]
    channels: List[ChannelBinding]
    pack_dir: Path
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def workspace(self) -> str:
        """Governor memory partition (``workspace_name``)."""
        return f"tenant-{self.slug}"

    def render_variables(self) -> Dict[str, Any]:
        """Wizard variables + the four derived structural variables.

        The derived values always win, so a contract cannot shadow ``workspace``
        or ``vertical`` with a stale hand-typed value.
        """
        merged: Dict[str, Any] = dict(self.variables)
        merged.update(
            {
                "tenant_slug": self.slug,
                "tenant_display_name": self.display_name,
                "vertical": self.vertical,
                "workspace": self.workspace,
            }
        )
        return merged

    def monthly_budget_cents(self) -> int:
        """Monthly cap in cents = daily_usd × 30 days × 100 cents."""
        return round(self.daily_usd * 30 * 100)


def _positive_float(value: Any) -> Optional[float]:
    """Coerce *value* to a positive float, or None if it isn't one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if f > 0 else None


def load_contract(path: Any, playbooks_root: Optional[Path] = None) -> TenantContract:
    """Parse + validate a tenant contract YAML into a :class:`TenantContract`.

    *playbooks_root* defaults to ``<repo>/playbooks``; override it in tests.
    Raises :class:`ContractError` (all problems in one message) on any issue,
    :class:`FileNotFoundError` if the file is absent, ``ContractError`` if it is
    not YAML.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"tenant contract not found: {p}")
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ContractError(f"{p}: not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise ContractError(f"{p}: contract must parse to a mapping")

    tenant = doc.get("tenant")
    if not isinstance(tenant, dict):
        raise ContractError(f"{p}: top-level 'tenant' mapping is required")

    root = Path(playbooks_root) if playbooks_root is not None else repo_playbooks_root()
    problems: List[str] = []

    slug = tenant.get("slug")
    if not isinstance(slug, str) or not slug:
        problems.append("tenant.slug is required")
        slug = ""
    elif not SLUG_RE.match(slug):
        problems.append(
            f"tenant.slug {slug!r} is not DNS-safe "
            "(^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$: lowercase alnum + hyphens, 3-40 chars)"
        )

    display_name = tenant.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        problems.append("tenant.display_name is required")
        display_name = ""

    vertical = tenant.get("vertical")
    pack_dir = root / str(vertical or "")
    if not isinstance(vertical, str) or not vertical:
        problems.append("tenant.vertical is required")
        vertical = ""
    elif not VERTICAL_RE.match(vertical):
        # aaf-0012: reject anything that isn't a plain pack name BEFORE any
        # filesystem I/O — a value like "../../etc" must never reach a path join.
        problems.append(
            f"tenant.vertical {vertical!r} is not a valid pack name "
            "(^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$: lowercase alnum + hyphens, 3-40 chars)"
        )
        vertical = ""
    else:
        # aaf-0012 defense-in-depth: realpath-contain the pack dir under the
        # playbooks root, so even a symlinked pack dir can't escape it.
        pack_dir = root / vertical
        root_real = root.resolve()
        pack_real = pack_dir.resolve()
        if pack_real != root_real and root_real not in pack_real.parents:
            problems.append(
                f"tenant.vertical {vertical!r} resolves outside the playbooks root"
            )
            vertical = ""
        elif not (pack_dir / "pack.yaml").is_file():
            problems.append(
                f"tenant.vertical {vertical!r}: playbook pack not found "
                f"(expected {pack_dir / 'pack.yaml'})"
            )

    budgets = tenant.get("budgets")
    daily_usd = per_run_usd = 0.0
    if not isinstance(budgets, dict):
        problems.append("tenant.budgets{daily_usd, per_run_usd} is required")
    else:
        d = _positive_float(budgets.get("daily_usd"))
        r = _positive_float(budgets.get("per_run_usd"))
        if d is None:
            problems.append("tenant.budgets.daily_usd must be a positive number")
        else:
            daily_usd = d
        if r is None:
            problems.append("tenant.budgets.per_run_usd must be a positive number")
        else:
            per_run_usd = r

    variables = tenant.get("variables", {})
    if variables is None:
        variables = {}
    if not isinstance(variables, dict):
        problems.append("tenant.variables must be a mapping")
        variables = {}
    else:
        for reserved in DERIVED_VARIABLES:
            if reserved in variables:
                problems.append(
                    f"tenant.variables must not set derived variable '{reserved}' "
                    "(the executor injects it from slug/display_name/vertical)"
                )

    channels_raw = tenant.get("channels", [])
    if channels_raw is None:
        channels_raw = []
    channels: List[ChannelBinding] = []
    if not isinstance(channels_raw, list):
        problems.append("tenant.channels must be a list")
    else:
        for i, ch in enumerate(channels_raw):
            if not isinstance(ch, dict):
                problems.append(f"tenant.channels[{i}] must be a mapping")
                continue
            kind = ch.get("kind")
            if not isinstance(kind, str) or not kind:
                problems.append(f"tenant.channels[{i}].kind is required")
                continue
            cfg = {k: v for k, v in ch.items() if k != "kind"}
            channels.append(ChannelBinding(kind=kind, config=cfg))

    if problems:
        raise ContractError(f"{p}: " + "; ".join(problems))

    return TenantContract(
        slug=slug,
        display_name=display_name,
        vertical=vertical,
        daily_usd=daily_usd,
        per_run_usd=per_run_usd,
        variables=dict(variables),
        channels=channels,
        pack_dir=pack_dir,
        raw=doc,
    )
