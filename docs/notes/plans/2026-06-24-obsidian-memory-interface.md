# Obsidian Memory Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use a subagent-driven development workflow (recommended) or a plan-execution workflow to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A two-way `memory ↔ Obsidian vault` CLI: `export` projects governed-memory entries into a local Obsidian-compatible Markdown+frontmatter vault, and `sync` applies operator edits back to the governor (delete → rm, confirm/pin/dispute/demote via frontmatter) with conflict reporting. Read (export) ships first and is a prerequisite for write-back.

**Architecture:** A new `governor.vault` module. Pure, fully-tested core: `render_note` (entry → Markdown), `parse_note` (Markdown → fields), `diff_to_actions` (baseline + vault → governor actions), `detect_conflicts` (baseline vs current server). Thin I/O wrappers: a `GovernorClient` (httpx) over the existing operator API (`GET /memory`, `GET /memory/{id}`, `POST /memory/{id}/action`), and `export()` / `sync()`. A CLI entry (`python -m governor.vault`). The vault never lives in Azure — the operator runs the CLI locally; conflicts (server changed since export) are reported and skipped, never overwritten.

**Tech Stack:** Python 3, httpx, pytest. Talks to the governor admin API the same way `pc-memory.sh` does.

---

## File Structure

- **Create** `services/memory-governor/src/governor/vault.py` — renderer, parser, diff, client, export/sync, CLI.
- **Create** `tests/memory-governor/test_vault.py` — pure-function tests + mock-client export/sync tests (auto-run by the existing `pytest -q tests/memory-governor` CI step; `conftest.py` already puts `governor` on `sys.path`).

**Governor API (from `services/memory-governor/src/governor/main.py`):**
- `GET /memory?...` → list rows: `id, snippet, memory_class, memory_scope_kind, memory_scope_id, source_type, created_by_peer, created_at, last_confirmed_at, expires_at`.
- `GET /memory/{id}` → full `documents` row (minus `embedding`): adds `content`, `verification_state`, `superseded_by`, `promotion_source_doc_id`, etc.
- `POST /memory/{id}/action` with `{action, actor, note?, demote_to?, superseded_by?}`; `action ∈ {pin, demote, confirm, dispute, supersede, rm, reconfirm}`.
- Auth: `X-Governor-Key` header (in-network, like `pc-memory.sh`) or the auth-proxy `/api/memory/*` Bearer path for a local operator. `GovernorClient` is configured from env so both work.

**Scope note:** body/content edits → `supersede` (which needs an `admit` + supersede two-step) are **out of scope for this plan** — a documented follow-on. This plan ships the read projection and the frontmatter/deletion-driven write-back verbs.

---

## Task 1: Pure note renderer (export half)

**Files:**
- Create: `services/memory-governor/src/governor/vault.py`
- Test: `tests/memory-governor/test_vault.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/memory-governor/test_vault.py`:

```python
"""Offline tests for the Obsidian memory-vault CLI: pure render/parse/diff plus
mock-client export/sync. No network — GovernorClient is faked."""

from governor import vault


ENTRY = {
    "id": "doc-1",
    "content": "Operator prefers symptom-first triage.\n\nSurface what's broken first.",
    "memory_class": "user_preference",
    "verification_state": "confirmed",
    "memory_scope_kind": "agent",
    "memory_scope_id": "orchestrator",
    "source_type": "operator",
    "created_by_peer": "orchestrator",
    "created_at": "2026-06-01T12:00:00Z",
    "last_confirmed_at": "2026-06-10T09:00:00Z",
    "expires_at": None,
    "superseded_by": None,
    "promotion_source_doc_id": None,
}


class TestRenderNote:
    def test_has_frontmatter_delimiters_and_body(self):
        out = vault.render_note(ENTRY)
        assert out.startswith("---\n")
        assert out.count("\n---\n") == 1  # exactly one closing delimiter
        assert "Operator prefers symptom-first triage." in out

    def test_frontmatter_fields(self):
        out = vault.render_note(ENTRY)
        assert "id: doc-1" in out
        assert "class: user_preference" in out
        assert "verification: confirmed" in out
        assert "scope_kind: agent" in out
        assert "scope_id: orchestrator" in out

    def test_omits_empty_fields(self):
        out = vault.render_note(ENTRY)
        assert "expires_at:" not in out  # None → omitted

    def test_renders_links_as_wikilinks(self):
        e = {**ENTRY, "superseded_by": "doc-9"}
        out = vault.render_note(e)
        assert "links:" in out
        assert '"[[doc-9]]"' in out

    def test_round_trips_through_parse(self):
        out = vault.render_note(ENTRY)
        parsed = vault.parse_note(out)  # defined in Task 3 — keep this test xfail until then
        assert parsed["id"] == "doc-1"
```

> The last test references `parse_note` (Task 3). Mark it skipped for now: add `import pytest` and decorate `test_round_trips_through_parse` with `@pytest.mark.skip(reason="parse_note lands in Task 3")`. Remove the skip in Task 3.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/memory-governor/test_vault.py::TestRenderNote -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'governor.vault'` (or `AttributeError: ... has no attribute 'render_note'`).

- [ ] **Step 3: Write minimal implementation**

Create `services/memory-governor/src/governor/vault.py`:

```python
"""memory ↔ Obsidian vault: project governed memory to a local Markdown vault
and apply operator edits back. Pure render/parse/diff + a thin httpx client.

The governed-memory six-class model maps 1:1 onto Markdown-note-with-frontmatter,
so an Obsidian vault is the front-end with no UI to build."""

from __future__ import annotations

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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/memory-governor/test_vault.py::TestRenderNote -v`
Expected: PASS (the round-trip test is skipped for now).

- [ ] **Step 5: Commit**

```bash
git add services/memory-governor/src/governor/vault.py tests/memory-governor/test_vault.py
git commit -m "feat(governor): vault render_note — memory entry to Obsidian note (Obsidian read)"
```

---

## Task 2: Governor client + export

**Files:**
- Modify: `services/memory-governor/src/governor/vault.py`
- Test: `tests/memory-governor/test_vault.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/memory-governor/test_vault.py`:

```python
import json


class FakeClient:
    """Stand-in for GovernorClient: serves canned list/show, records actions."""
    def __init__(self, entries):
        self._entries = {e["id"]: e for e in entries}
        self.actions = []

    def list_memory(self):
        return [{"id": e["id"]} for e in self._entries.values()]

    def show(self, doc_id):
        return self._entries[doc_id]

    def action(self, doc_id, body):
        self.actions.append((doc_id, body))
        return {"ok": True}


class TestExport:
    def test_writes_one_note_per_entry_plus_baseline(self, tmp_path):
        client = FakeClient([ENTRY, {**ENTRY, "id": "doc-2", "content": "second"}])
        written = vault.export(client, tmp_path)
        assert written == 2
        assert (tmp_path / "doc-1.md").exists()
        assert (tmp_path / "doc-2.md").exists()
        assert "symptom-first" in (tmp_path / "doc-1.md").read_text()
        # baseline snapshot records id→{class,verification} for the diff later
        baseline = json.loads((tmp_path / vault.BASELINE_FILE).read_text())
        assert baseline["doc-1"]["class"] == "user_preference"
        assert baseline["doc-1"]["verification"] == "confirmed"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/memory-governor/test_vault.py::TestExport -v`
Expected: FAIL — `AttributeError: module 'governor.vault' has no attribute 'export'` (and `BASELINE_FILE`).

- [ ] **Step 3: Write minimal implementation**

Append to `services/memory-governor/src/governor/vault.py`:

```python
import json
import os
from pathlib import Path

import httpx

BASELINE_FILE = ".governor-baseline.json"


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
            return cls(proxy, {"Authorization": f"Bearer {os.environ['MEMORY_API_TOKEN']}"}, prefix="/api")
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/memory-governor/test_vault.py::TestExport -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/memory-governor/src/governor/vault.py tests/memory-governor/test_vault.py
git commit -m "feat(governor): vault GovernorClient + export with baseline snapshot (Obsidian read)"
```

---

## Task 3: Note parser + edit→action diff (write-back core)

**Files:**
- Modify: `services/memory-governor/src/governor/vault.py`
- Test: `tests/memory-governor/test_vault.py`

- [ ] **Step 1: Write the failing test**

In `tests/memory-governor/test_vault.py`, remove the `@pytest.mark.skip` from `test_round_trips_through_parse`, then append:

```python
class TestParseNote:
    def test_extracts_frontmatter_fields(self):
        note = vault.render_note({**ENTRY, "memory_class": "durable_fact",
                                  "verification_state": "unverified"})
        p = vault.parse_note(note)
        assert p["id"] == "doc-1"
        assert p["class"] == "durable_fact"
        assert p["verification"] == "unverified"

    def test_missing_frontmatter_returns_empty(self):
        assert vault.parse_note("no frontmatter here") == {}


class TestDiffToActions:
    def _baseline(self):
        return {
            "doc-1": {"class": "user_preference", "verification": "unverified"},
            "doc-2": {"class": "durable_fact", "verification": "confirmed"},
        }

    def test_deleted_note_becomes_rm(self):
        # doc-2 absent from the vault → rm
        actions = vault.diff_to_actions(self._baseline(), {"doc-1": {"id": "doc-1",
            "class": "user_preference", "verification": "unverified"}})
        assert {"doc_id": "doc-2", "action": "rm"} in actions

    def test_confirm_when_verification_flipped(self):
        current = {
            "doc-1": {"id": "doc-1", "class": "user_preference", "verification": "confirmed"},
            "doc-2": {"id": "doc-2", "class": "durable_fact", "verification": "confirmed"},
        }
        actions = vault.diff_to_actions(self._baseline(), current)
        assert {"doc_id": "doc-1", "action": "confirm"} in actions

    def test_pin_when_class_set_to_pinned(self):
        current = {
            "doc-1": {"id": "doc-1", "class": "pinned", "verification": "unverified"},
            "doc-2": {"id": "doc-2", "class": "durable_fact", "verification": "confirmed"},
        }
        actions = vault.diff_to_actions(self._baseline(), current)
        assert {"doc_id": "doc-1", "action": "pin"} in actions

    def test_demote_records_target_class(self):
        current = {
            "doc-1": {"id": "doc-1", "class": "user_preference", "verification": "unverified"},
            "doc-2": {"id": "doc-2", "class": "decaying", "verification": "confirmed"},
        }
        actions = vault.diff_to_actions(self._baseline(), current)
        assert {"doc_id": "doc-2", "action": "demote", "demote_to": "decaying"} in actions

    def test_no_change_yields_no_actions(self):
        current = {
            "doc-1": {"id": "doc-1", "class": "user_preference", "verification": "unverified"},
            "doc-2": {"id": "doc-2", "class": "durable_fact", "verification": "confirmed"},
        }
        assert vault.diff_to_actions(self._baseline(), current) == []


class TestDetectConflicts:
    def test_flags_entry_changed_on_server_since_export(self):
        baseline = {"doc-1": {"class": "durable_fact", "verification": "unverified"}}
        server = {"doc-1": {"class": "durable_fact", "verification": "confirmed"}}  # moved
        assert vault.detect_conflicts(baseline, server) == ["doc-1"]

    def test_no_conflict_when_unchanged(self):
        baseline = {"doc-1": {"class": "durable_fact", "verification": "unverified"}}
        server = {"doc-1": {"class": "durable_fact", "verification": "unverified"}}
        assert vault.detect_conflicts(baseline, server) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/memory-governor/test_vault.py::TestParseNote tests/memory-governor/test_vault.py::TestDiffToActions tests/memory-governor/test_vault.py::TestDetectConflicts -v`
Expected: FAIL — `parse_note` / `diff_to_actions` / `detect_conflicts` undefined.

- [ ] **Step 3: Write minimal implementation**

Append to `services/memory-governor/src/governor/vault.py`:

```python
_DEMOTE_TARGETS = ("durable_fact", "user_preference", "task_scoped", "decaying")


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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/memory-governor/test_vault.py -v`
Expected: PASS — including the now-unskipped round-trip test.

- [ ] **Step 5: Commit**

```bash
git add services/memory-governor/src/governor/vault.py tests/memory-governor/test_vault.py
git commit -m "feat(governor): vault parse_note + diff_to_actions + detect_conflicts (Obsidian write-back core)"
```

---

## Task 4: sync (apply write-back with conflict skipping)

**Files:**
- Modify: `services/memory-governor/src/governor/vault.py`
- Test: `tests/memory-governor/test_vault.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/memory-governor/test_vault.py`:

```python
class TestSync:
    def _seed_vault(self, tmp_path, entries):
        client = FakeClient(entries)
        vault.export(client, tmp_path)
        return client

    def test_deleting_a_note_applies_rm(self, tmp_path):
        client = self._seed_vault(tmp_path, [ENTRY, {**ENTRY, "id": "doc-2", "content": "x"}])
        (tmp_path / "doc-2.md").unlink()  # operator deletes the note
        result = vault.sync(client, tmp_path, actor="operator")
        assert ("doc-2", {"action": "rm", "actor": "operator"}) in client.actions
        assert result["applied"] == 1
        assert result["conflicts"] == []

    def test_confirm_via_frontmatter_edit(self, tmp_path):
        unconf = {**ENTRY, "verification_state": "unverified"}
        client = self._seed_vault(tmp_path, [unconf])
        # operator edits the note's frontmatter to confirmed
        note = (tmp_path / "doc-1.md").read_text().replace(
            "verification: unverified", "verification: confirmed")
        (tmp_path / "doc-1.md").write_text(note)
        vault.sync(client, tmp_path, actor="operator")
        assert any(a == ("doc-1", {"action": "confirm", "actor": "operator"}) for a in client.actions)

    def test_conflict_is_skipped_and_reported(self, tmp_path):
        unconf = {**ENTRY, "verification_state": "unverified"}
        client = self._seed_vault(tmp_path, [unconf])
        # server moved doc-1 to confirmed AFTER export (someone else changed it)
        client._entries["doc-1"]["verification_state"] = "confirmed"
        # operator tries to dispute it locally
        note = (tmp_path / "doc-1.md").read_text().replace(
            "verification: unverified", "verification: disputed")
        (tmp_path / "doc-1.md").write_text(note)
        result = vault.sync(client, tmp_path, actor="operator")
        assert result["conflicts"] == ["doc-1"]
        assert client.actions == []  # nothing applied for the conflicted doc
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/memory-governor/test_vault.py::TestSync -v`
Expected: FAIL — `AttributeError: module 'governor.vault' has no attribute 'sync'`.

- [ ] **Step 3: Write minimal implementation**

Append to `services/memory-governor/src/governor/vault.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/memory-governor/test_vault.py -v`
Expected: PASS (all vault tests).

- [ ] **Step 5: Commit**

```bash
git add services/memory-governor/src/governor/vault.py tests/memory-governor/test_vault.py
git commit -m "feat(governor): vault sync — apply edits with conflict skipping (Obsidian write-back)"
```

---

## Task 5: CLI entry point

**Files:**
- Modify: `services/memory-governor/src/governor/vault.py`
- Test: `tests/memory-governor/test_vault.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/memory-governor/test_vault.py`:

```python
class TestCli:
    def test_export_dispatch_calls_export(self, tmp_path, monkeypatch):
        calls = {}
        monkeypatch.setattr(vault, "_client_from_env", lambda: "CLIENT")
        monkeypatch.setattr(vault, "export", lambda client, d: calls.setdefault("export", (client, str(d))) or 3)
        rc = vault.main(["export", str(tmp_path)])
        assert rc == 0
        assert calls["export"] == ("CLIENT", str(tmp_path))

    def test_unknown_command_returns_2(self):
        assert vault.main(["frobnicate", "x"]) == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/memory-governor/test_vault.py::TestCli -v`
Expected: FAIL — `vault.main` / `vault._client_from_env` undefined.

- [ ] **Step 3: Write minimal implementation**

Append to `services/memory-governor/src/governor/vault.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/memory-governor/test_vault.py -v`
Expected: PASS (all vault tests, incl. CLI).

- [ ] **Step 5: Commit**

```bash
git add services/memory-governor/src/governor/vault.py tests/memory-governor/test_vault.py
git commit -m "feat(governor): vault CLI entry (python -m governor.vault export|sync) (Obsidian)"
```

---

## Self-Review

**Spec coverage** (against §2.4 of the v1.3 design):
- Pure renderer (entry → Markdown + frontmatter: class/verification/scope/source/timestamps + wikilinks) → Task 1. ✅
- `export` → local Obsidian vault via a command, on the governor operator API → Tasks 2 + 5. ✅
- `sync` write-back: delete → rm; trust/confirmed frontmatter → confirm/pin; class → demote → Tasks 3 + 4. ✅
- Conflict policy: governor source-of-truth, conflicts reported and skipped, baseline snapshot taken at export → Tasks 2 (baseline) + 3 (`detect_conflicts`) + 4 (sync skips). ✅
- Local CLI delivery (no Azure infra), env-configured for in-network or auth-proxy → Task 2 (`from_env`) + Task 5. ✅
- Operator-gated / privileged (no agent path) → the CLI is operator-run and not imported by any agent/service code. ✅
- **Documented out-of-scope:** body/content edits → `supersede` (needs admit + supersede) — called out in the File Structure note as a follow-on, consistent with the spec's "write-back may fast-follow".

**Placeholder scan:** none — every function and test has complete code. The Task 1 round-trip test's temporary `@pytest.mark.skip` is explicitly removed in Task 3 (a real instruction, not a placeholder).

**Type consistency:** `render_note(entry) -> str`, `parse_note(text) -> dict`, `diff_to_actions(baseline, current) -> list[dict]`, `detect_conflicts(baseline, server) -> list[str]`, `export(client, dir) -> int`, `sync(client, dir, *, actor) -> dict` are used identically in their tests and call sites. The baseline shape `{id: {"class", "verification"}}` is produced by `_baseline_entry` and consumed identically by `diff_to_actions`/`detect_conflicts`. The action dict keys (`doc_id`, `action`, `demote_to`) match between `diff_to_actions` and `sync`'s body construction, and the action strings match the governor's `VALID_ACTIONS`.
