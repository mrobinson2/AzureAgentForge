"""Offline tests for contradiction detection — pure units."""

from datetime import datetime, timezone, timedelta

from governor import contradiction, llm

T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestParseContradictionOutcome:
    def test_clean_words(self):
        for w in ("none", "supersede", "scope_refine", "coexist", "needs_review"):
            assert llm.parse_contradiction_outcome(w) == w

    def test_case_and_punctuation_tolerant(self):
        assert llm.parse_contradiction_outcome("Supersede.") == "supersede"
        assert llm.parse_contradiction_outcome("  NEEDS_REVIEW\n") == "needs_review"
        assert llm.parse_contradiction_outcome("scope_refine — because ...") == "scope_refine"

    def test_noise_defaults_to_none(self):
        assert llm.parse_contradiction_outcome("") == "none"
        assert llm.parse_contradiction_outcome("   ") == "none"
        assert llm.parse_contradiction_outcome("banana") == "none"


class TestFlaggingPolicy:
    def test_only_real_conflicts_flag(self):
        assert "supersede" in contradiction.FLAGGING_OUTCOMES
        assert "scope_refine" in contradiction.FLAGGING_OUTCOMES
        assert "needs_review" in contradiction.FLAGGING_OUTCOMES
        # coexist / none never flag
        assert "coexist" not in contradiction.FLAGGING_OUTCOMES
        assert "none" not in contradiction.FLAGGING_OUTCOMES


class TestPickLoser:
    def _pair(self, a_trust, b_trust, a_created=T0, b_created=T0):
        return {"a_id": "A", "b_id": "B", "a_trust": a_trust, "b_trust": b_trust,
                "a_created": a_created, "b_created": b_created}

    def test_lower_trust_loses(self):
        assert contradiction._pick_loser(self._pair(0.4, 0.9)) == ("A", "B")
        assert contradiction._pick_loser(self._pair(0.9, 0.4)) == ("B", "A")

    def test_trust_tie_older_loses(self):
        older, newer = T0 - timedelta(days=10), T0
        assert contradiction._pick_loser(
            self._pair(0.6, 0.6, a_created=older, b_created=newer)
        ) == ("A", "B")
        assert contradiction._pick_loser(
            self._pair(0.6, 0.6, a_created=newer, b_created=older)
        ) == ("B", "A")
