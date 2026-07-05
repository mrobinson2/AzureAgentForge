"""memory ↔ Obsidian vault: project governed memory to a local Markdown vault
and apply operator edits back. Pure render/parse/diff + a thin httpx client.

The governed-memory six-class model maps 1:1 onto Markdown-note-with-frontmatter,
so an Obsidian vault is the front-end with no UI to build."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

BASELINE_FILE = ".governor-baseline.json"
_DEMOTE_TARGETS = ("durable_fact", "user_preference", "task_scoped", "decaying")

# Frontmatter key → source field on the governor `documents` row.
_FIELD_MAP = [
    ("id", "id"),
    ("class", "memory_class"),
    ("verification", "verification_state"),
    ("scope_kind", "memory_scope_kind"),
    ("scope_id", "memory_scope_id"),
    ("source", "source_type"),
    ("created_by", "created_by_peer"),
    ("created_at", "created_at"),
    ("last_confirmed_at", "last_confirmed_at"),
    ("expires_at", "expires_at"),
]


def _scalar(value) -> str:
    """Emit a frontmatter scalar; quote when it could be misread."""
    s = str(value)
    if s == "" or any(c in s for c in ':#"\n') or s.strip() != s:
        return '"' + s.replace('"', '\\"') + '"'
    return s


def render_note(entry: dict) -> str:
    """Render a governor memory entry as an Obsidian Markdown note (frontmatter
    + body). Empty/None fields are omitted; relations become wikilinks."""
    lines = ["---"]
    for key, field in _FIELD_MAP:
        val = entry.get(field)
        if val is not None and val != "":
            lines.append(f"{key}: {_scalar(val)}")

    links = []
    for field in ("superseded_by", "promotion_source_doc_id"):
        ref = entry.get(field)
        if ref and ref != entry.get("id"):
            links.append(ref)
    if links:
        lines.append("links:")
        lines.extend(f'  - "[[{ref}]]"' for ref in links)

    lines.append("---")
    lines.append("")
    lines.append((entry.get("content") or "").rstrip())
    lines.append("")
    return "\n".join(lines)


class GovernorClient:
    """Thin client over the governor operator API. Configured from env so it
    works both in-network (X-Governor-Key) and via the auth-proxy (Bearer)."""

    def __init__(self, base_url: str, headers: dict, prefix: str = ""):
        self._base = base_url.rstrip("/")
        self._prefix = prefix
        self._headers = headers

    @classmethod
    def from_env(cls) -> "GovernorClient":
        # In-network default (mirrors pc-memory.sh). For a local operator, set
        # MEMORY_API_BASE_URL (control-plane host) + MEMORY_API_TOKEN (JWT).
        proxy = os.environ.get("MEMORY_API_BASE_URL")
        if proxy:
            return cls(proxy, {"Authorization": f"Bearer {os.environ.get('MEMORY_API_TOKEN', '')}"}, prefix="/api")
        base = os.environ.get("GOVERNOR_BASE_URL", "http://ca-memory-governor-dev")
        return cls(base, {"X-Governor-Key": os.environ.get("GOVERNOR_API_KEY", "")})

    def _url(self, path: str) -> str:
        return f"{self._base}{self._prefix}{path}"

    def list_memory(self) -> list[dict]:
        r = httpx.get(self._url("/memory"), headers=self._headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def show(self, doc_id: str) -> dict:
        r = httpx.get(self._url(f"/memory/{doc_id}"), headers=self._headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def action(self, doc_id: str, body: dict) -> dict:
        r = httpx.post(self._url(f"/memory/{doc_id}/action"), headers=self._headers, json=body, timeout=30)
        r.raise_for_status()
        return r.json()


def _baseline_entry(entry: dict) -> dict:
    return {"class": entry.get("memory_class"), "verification": entry.get("verification_state")}


def export(client, vault_dir) -> int:
    """Project all governed memory into vault_dir as Obsidian notes, and write a
    baseline snapshot used by sync() to detect local edits. Returns the count."""
    vault_dir = Path(vault_dir)
    vault_dir.mkdir(parents=True, exist_ok=True)
    baseline: dict[str, dict] = {}
    count = 0
    for ref in client.list_memory():
        entry = client.show(ref["id"])
        (vault_dir / f"{entry['id']}.md").write_text(render_note(entry), encoding="utf-8")
        baseline[entry["id"]] = _baseline_entry(entry)
        count += 1
    (vault_dir / BASELINE_FILE).write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    return count


def parse_note(text: str) -> dict:
    """Inverse of render_note's frontmatter: returns the key→value scalar map.
    Empty dict when the note has no leading frontmatter block."""
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line or line.startswith("  ") or line.rstrip() == "links:":
            continue  # skip list items / the links header
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1].replace('\\"', '"')
        out[key.strip()] = val
    return out


def diff_to_actions(baseline: dict, current: dict) -> list[dict]:
    """Map vault edits to governor actions. `baseline` is id→{class,verification}
    from the last export; `current` is id→parsed-note. Body/content edits are NOT
    handled here (supersede is a documented follow-on)."""
    actions: list[dict] = []
    for doc_id, base in baseline.items():
        cur = current.get(doc_id)
        if cur is None:
            actions.append({"doc_id": doc_id, "action": "rm"})
            continue
        cur_class = cur.get("class")
        cur_verif = cur.get("verification")
        if cur_class == "pinned" and base.get("class") != "pinned":
            actions.append({"doc_id": doc_id, "action": "pin"})
        elif cur_verif == "confirmed" and base.get("verification") != "confirmed":
            actions.append({"doc_id": doc_id, "action": "confirm"})
        elif cur_verif == "disputed" and base.get("verification") != "disputed":
            actions.append({"doc_id": doc_id, "action": "dispute"})
        elif cur_class != base.get("class") and cur_class in _DEMOTE_TARGETS:
            actions.append({"doc_id": doc_id, "action": "demote", "demote_to": cur_class})
    return actions


def detect_conflicts(baseline: dict, server: dict) -> list[str]:
    """Ids whose server state changed since export (class or verification differ
    from baseline) — these are skipped at sync time to avoid clobbering."""
    conflicts = []
    for doc_id, base in baseline.items():
        srv = server.get(doc_id)
        if srv is None:
            continue  # deleted server-side; a vault rm on it is a no-op/handled elsewhere
        if srv.get("class") != base.get("class") or srv.get("verification") != base.get("verification"):
            conflicts.append(doc_id)
    return conflicts


def _read_baseline(vault_dir: Path) -> dict:
    p = vault_dir / BASELINE_FILE
    if not p.exists():
        raise FileNotFoundError(f"no baseline at {p} — run `export` first")
    return json.loads(p.read_text(encoding="utf-8"))


def _read_vault(vault_dir: Path) -> dict:
    """id → parsed-note for every *.md in the vault (excludes the baseline)."""
    current = {}
    for md in vault_dir.glob("*.md"):
        parsed = parse_note(md.read_text(encoding="utf-8"))
        if parsed.get("id"):
            current[parsed["id"]] = parsed
    return current


def sync(client, vault_dir, *, actor: str = "operator") -> dict:
    """Apply vault edits back to the governor. Re-fetches current server state to
    skip conflicts (entries changed since export). Returns {applied, conflicts}."""
    vault_dir = Path(vault_dir)
    baseline = _read_baseline(vault_dir)
    current = _read_vault(vault_dir)

    server = {ref["id"]: _baseline_entry(client.show(ref["id"]))
              for ref in client.list_memory()}
    conflicts = set(detect_conflicts(baseline, server))

    applied = 0
    for act in diff_to_actions(baseline, current):
        if act["doc_id"] in conflicts:
            continue
        body = {"action": act["action"], "actor": actor}
        if act["action"] == "demote":
            body["demote_to"] = act["demote_to"]
        client.action(act["doc_id"], body)
        applied += 1
    return {"applied": applied, "conflicts": sorted(conflicts)}


def _client_from_env():
    return GovernorClient.from_env()


def main(argv: list[str] | None = None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] not in ("export", "sync"):
        print("usage: python -m governor.vault {export|sync} <vault-dir>", file=sys.stderr)
        return 2
    cmd, vault_dir = args[0], args[1]
    client = _client_from_env()
    if cmd == "export":
        n = export(client, vault_dir)
        print(f"exported {n} notes to {vault_dir}")
    else:
        result = sync(client, vault_dir)
        print(f"applied {result['applied']} change(s); "
              f"{len(result['conflicts'])} conflict(s) skipped: {result['conflicts']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
