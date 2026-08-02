"""Unit tests for kill_switch.py — the scoped paid-action switch.

The property under test throughout is *scoping*. A kill switch that stops
everything is easy to build and useless in an incident: it turns a cost
problem into an outage. These tests pin down what each scope does and, just
as importantly, what it must leave alone.
"""

import pytest

import kill_switch as ks


def intent(tier="gpt4o-mini", *, metered=True, is_fallback=False, primary="gpt4o-mini"):
    return ks.DispatchIntent(
        tier=tier, metered=metered, is_fallback=is_fallback, primary_tier=primary,
    )


# ── Scope parsing ─────────────────────────────────────────────────────────────

class TestParseScopes:
    def test_empty_is_disengaged(self):
        assert ks.parse_scopes(None) == (set(), [])
        assert ks.parse_scopes("") == (set(), [])
        assert ks.parse_scopes("  ,, ") == (set(), [])

    def test_recognized_scopes(self):
        found, unknown = ks.parse_scopes("paid_fallback, all_paid")
        assert found == {ks.SCOPE_PAID_FALLBACK, ks.SCOPE_ALL_PAID}
        assert unknown == []

    def test_case_and_whitespace_tolerant(self):
        found, _ = ks.parse_scopes(" ALL_PAID ")
        assert found == {ks.SCOPE_ALL_PAID}

    def test_unknown_names_are_returned_not_raised(self):
        """A typo must not stop the router booting — but it must not silently
        look like a working kill switch either, so the host can log it."""
        found, unknown = ks.parse_scopes("paid_fallback,all_pald")
        assert found == {ks.SCOPE_PAID_FALLBACK}
        assert unknown == ["all_pald"]


# ── Engagement state ──────────────────────────────────────────────────────────

class TestEngagement:
    def test_default_is_disengaged(self):
        switch = ks.KillSwitch()
        assert switch.engaged_scopes() == []
        for scope in ks.ALL_SCOPES:
            assert switch.is_engaged(scope) is False

    def test_boot_scopes_are_honored(self):
        switch = ks.KillSwitch({ks.SCOPE_PAID_FALLBACK})
        assert switch.is_engaged(ks.SCOPE_PAID_FALLBACK) is True
        assert switch.is_engaged(ks.SCOPE_ALL_PAID) is False

    def test_engage_and_release_round_trip(self):
        switch = ks.KillSwitch()
        switch.engage(ks.SCOPE_ALL_PAID, actor="oncall", reason="bill alert")
        assert switch.is_engaged(ks.SCOPE_ALL_PAID) is True
        switch.release(ks.SCOPE_ALL_PAID, actor="oncall", reason="fixed")
        assert switch.is_engaged(ks.SCOPE_ALL_PAID) is False

    def test_unknown_scope_is_rejected(self):
        switch = ks.KillSwitch()
        with pytest.raises(ValueError):
            switch.engage("everything")

    def test_reset_returns_to_boot_posture_not_to_empty(self):
        switch = ks.KillSwitch({ks.SCOPE_PAID_FALLBACK})
        switch.engage(ks.SCOPE_ALL_PAID)
        switch.release(ks.SCOPE_PAID_FALLBACK)
        switch.reset()
        assert switch.engaged_scopes() == [ks.SCOPE_PAID_FALLBACK]


class TestEvents:
    def test_engage_and_release_emit_events(self):
        events = []
        switch = ks.KillSwitch(on_event=events.append)
        switch.engage(ks.SCOPE_PAID_FALLBACK, actor="oncall", reason="runaway agent")
        switch.release(ks.SCOPE_PAID_FALLBACK, actor="oncall", reason="agent stopped")

        assert [e["event"] for e in events] == [ks.EVENT_ENGAGED, ks.EVENT_RELEASED]
        assert events[0]["actor"] == "oncall"
        assert events[0]["reason"] == "runaway agent"
        assert events[0]["engaged_scopes"] == [ks.SCOPE_PAID_FALLBACK]
        assert events[1]["engaged_scopes"] == []

    def test_no_op_flip_is_reported_as_unchanged(self):
        events = []
        switch = ks.KillSwitch(on_event=events.append)
        switch.engage(ks.SCOPE_ALL_PAID)
        switch.engage(ks.SCOPE_ALL_PAID)
        assert events[0]["changed"] is True
        assert events[1]["changed"] is False

    def test_event_sink_failure_never_breaks_the_switch(self):
        def boom(_event):
            raise RuntimeError("recorder down")

        switch = ks.KillSwitch(on_event=boom)
        switch.engage(ks.SCOPE_ALL_PAID)
        assert switch.is_engaged(ks.SCOPE_ALL_PAID) is True


# ── The decision itself ───────────────────────────────────────────────────────

class TestEvaluate:
    def test_disengaged_allows_everything(self):
        switch = ks.KillSwitch()
        assert switch.evaluate(intent()).blocked is False
        assert switch.evaluate(intent(is_fallback=True)).blocked is False

    def test_paid_fallback_blocks_only_the_fallback_hop(self):
        switch = ks.KillSwitch({ks.SCOPE_PAID_FALLBACK})
        # The caller's originally selected tier is ordinary traffic, not an
        # incident — this scope must never touch it.
        assert switch.evaluate(intent(is_fallback=False)).blocked is False
        decision = switch.evaluate(intent(tier="phi4", is_fallback=True))
        assert decision.blocked is True
        assert decision.scope == ks.SCOPE_PAID_FALLBACK

    def test_all_paid_blocks_primary_and_fallback(self):
        switch = ks.KillSwitch({ks.SCOPE_ALL_PAID})
        for is_fallback in (False, True):
            decision = switch.evaluate(intent(is_fallback=is_fallback))
            assert decision.blocked is True
            assert decision.scope == ks.SCOPE_ALL_PAID

    @pytest.mark.parametrize("scope", ks.ALL_SCOPES)
    @pytest.mark.parametrize("is_fallback", [False, True])
    def test_free_tiers_are_never_blocked(self, scope, is_fallback):
        """Local inference on hardware the operator already owns has zero
        marginal cost. Blocking it would turn a spend control into an outage
        for the one path that costs nothing."""
        switch = ks.KillSwitch({scope})
        decision = switch.evaluate(
            intent(tier="phi4-local", metered=False, is_fallback=is_fallback)
        )
        assert decision.blocked is False

    def test_all_paid_wins_when_both_scopes_are_engaged(self):
        switch = ks.KillSwitch(set(ks.ALL_SCOPES))
        assert switch.evaluate(intent(is_fallback=True)).scope == ks.SCOPE_ALL_PAID

    def test_blocked_counts_are_per_scope(self):
        switch = ks.KillSwitch({ks.SCOPE_PAID_FALLBACK})
        switch.evaluate(intent(is_fallback=True))
        switch.evaluate(intent(is_fallback=True))
        switch.evaluate(intent(is_fallback=False))  # allowed, not counted
        snap = switch.snapshot()
        assert snap["blocked_counts"][ks.SCOPE_PAID_FALLBACK] == 2
        assert snap["blocked_counts"][ks.SCOPE_ALL_PAID] == 0


class TestSnapshotAndDetail:
    def test_snapshot_shape(self):
        switch = ks.KillSwitch({ks.SCOPE_ALL_PAID})
        snap = switch.snapshot()
        assert snap["scopes"] == {ks.SCOPE_PAID_FALLBACK: False, ks.SCOPE_ALL_PAID: True}
        assert snap["engaged"] == [ks.SCOPE_ALL_PAID]
        assert snap["boot_engaged"] == [ks.SCOPE_ALL_PAID]

    def test_block_detail_names_the_switch(self):
        switch = ks.KillSwitch({ks.SCOPE_PAID_FALLBACK})
        target = intent(tier="phi4", is_fallback=True, primary="gpt4o-mini")
        decision = switch.evaluate(target)
        detail = ks.block_detail(decision, target)
        assert detail["code"] == ks.KILL_SWITCH_ERROR_CODE
        assert detail["kill_switch_scope"] == ks.SCOPE_PAID_FALLBACK
        assert detail["tier"] == "phi4"
        assert detail["primary_tier"] == "gpt4o-mini"
        assert detail["is_fallback"] is True
        # Only an operator releasing the scope changes this answer, so a
        # client retry accomplishes nothing.
        assert detail["retryable"] is False
