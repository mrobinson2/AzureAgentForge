"""memoryProfile enforcement tests."""

from governor.profiles import can_write, profile_for, readable_classes


class TestWriteAuthority:
    def test_orchestrator_writes_all(self):
        for c in ("durable_fact", "user_preference", "task_scoped", "ephemeral", "decaying"):
            assert can_write("orchestrator", c)

    def test_specialist_limited_to_task_and_ephemeral(self):
        assert can_write("coder", "task_scoped")
        assert can_write("coder", "ephemeral")
        assert not can_write("coder", "durable_fact")
        assert not can_write("coder", "user_preference")
        assert not can_write("coder", "decaying")

    def test_security_ephemeral_only(self):
        assert can_write("security", "ephemeral")
        assert not can_write("security", "task_scoped")

    def test_cost_guardian_decaying_only(self):
        assert can_write("cost-guardian", "decaying")
        assert not can_write("cost-guardian", "ephemeral")

    def test_unknown_writer_gets_specialist(self):
        assert profile_for("totally-new-agent")["write"] == ["task_scoped", "ephemeral"]

    def test_system_writers_unrestricted(self):
        for who in ("operator", "annotator", "system", "sweeper"):
            assert can_write(who, "durable_fact")

    def test_watchdog_writes_durable_fact_least_privilege(self):
        # The watchdog's self-improvement-loop writer: durable_fact authority so
        # lessons persist, but nothing more.
        assert can_write("watchdog", "durable_fact")
        assert can_write("watchdog", "decaying")
        assert not can_write("watchdog", "task_scoped")
        assert not can_write("watchdog", "user_preference")
        assert not can_write("watchdog", "ephemeral")


class TestReadAuthority:
    def test_everyone_reads_pinned(self):
        for slug in ("orchestrator", "coder", "security", "cost-guardian", "unknown"):
            assert "pinned" in readable_classes(slug)

    def test_specialist_cannot_read_preferences(self):
        assert "user_preference" not in readable_classes("researcher")

    def test_orchestrator_reads_all(self):
        assert len(readable_classes("orchestrator")) == 6

    def test_watchdog_reads_durable_fact(self):
        assert "durable_fact" in readable_classes("watchdog")
