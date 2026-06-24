"""Offline tests for the Obsidian memory-vault CLI: pure render/parse/diff plus
mock-client export/sync. No network — GovernorClient is faked."""

import pytest
from governor import vault


ENTRY = {
    "id": "doc-1",
    "content": "Michael prefers symptom-first triage.\n\nSurface what's broken first.",
    "memory_class": "user_preference",
    "verification_state": "confirmed",
    "memory_scope_kind": "agent",
    "memory_scope_id": "alfred",
    "source_type": "operator",
    "created_by_peer": "alfred",
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
        assert "Michael prefers symptom-first triage." in out

    def test_frontmatter_fields(self):
        out = vault.render_note(ENTRY)
        assert "id: doc-1" in out
        assert "class: user_preference" in out
        assert "verification: confirmed" in out
        assert "scope_kind: agent" in out
        assert "scope_id: alfred" in out

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
