"""Tests for the Key Vault secret-expiry detector (detect_expiring_secrets).

Security-relevant and previously untested. Pure function — the caller lists the
vault and supplies `now`, so every boundary is exercised offline.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.watchdog import detectors  # noqa: E402

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _sec(name, days_from_now):
    return {"name": name, "expires_on": NOW + timedelta(days=days_from_now)}


def test_expired_secret_is_critical():
    out = detectors.detect_expiring_secrets([_sec("db-password", -3)], now=NOW)
    assert len(out) == 1
    f = out[0]
    assert f.severity == "critical"
    assert f.signature == "secret-expiry:db-password"
    assert f.recommended_owner == "Security"
    assert f.evidence["secret"] == "db-password"
    assert f.evidence["days_until_expiry"] == -3.0


def test_secret_expiring_within_warn_window_is_high():
    out = detectors.detect_expiring_secrets([_sec("api-key", 5)], now=NOW)
    assert len(out) == 1 and out[0].severity == "high"
    assert "5 day(s)" in out[0].title


def test_healthy_secret_far_out_produces_nothing():
    assert detectors.detect_expiring_secrets([_sec("cert", 90)], now=NOW) == []


def test_secret_with_no_expiry_is_skipped():
    assert detectors.detect_expiring_secrets(
        [{"name": "never-expires", "expires_on": None}], now=NOW) == []
    # a missing key behaves like None, too
    assert detectors.detect_expiring_secrets([{"name": "x"}], now=NOW) == []


def test_boundary_exactly_at_warn_days_is_flagged():
    out = detectors.detect_expiring_secrets(
        [_sec("edge", detectors.SECRET_EXPIRY_WARN_DAYS)], now=NOW)
    assert len(out) == 1 and out[0].severity == "high"


def test_boundary_just_past_warn_days_is_clean():
    out = detectors.detect_expiring_secrets(
        [_sec("safe", detectors.SECRET_EXPIRY_WARN_DAYS + 1)], now=NOW)
    assert out == []


def test_custom_warn_days_widens_the_window():
    secs = [_sec("rotate-soon", 20)]
    assert detectors.detect_expiring_secrets(secs, now=NOW) == []        # default 14
    out = detectors.detect_expiring_secrets(secs, now=NOW, warn_days=30)  # widened
    assert len(out) == 1 and out[0].severity == "high"


def test_mixed_vault_flags_only_at_risk_and_dedups_per_secret():
    secrets = [
        _sec("expired-1", -1),     # critical
        _sec("soon-1", 3),         # high
        _sec("healthy-1", 200),    # clean
        {"name": "no-exp", "expires_on": None},  # skipped
    ]
    out = detectors.detect_expiring_secrets(secrets, now=NOW)
    assert sorted(f.severity for f in out) == ["critical", "high"]
    # each at-risk secret gets its own stable signature
    assert {f.signature for f in out} == {"secret-expiry:expired-1", "secret-expiry:soon-1"}
