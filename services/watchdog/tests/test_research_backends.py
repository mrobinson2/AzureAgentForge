"""Tests for the research-backend health detector (detect_research_backends).

Researcher agents depend on a set of web backends (search, page-read,
video-transcript). When one goes down they fail indirectly — empty results read
as "nothing found", or a silent fallback/cancel. Pure function: the caller runs
the probes and supplies [{name, ok, detail}], so every branch is offline-testable.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.watchdog import detectors  # noqa: E402


def test_down_backend_is_high_with_stable_signature():
    out = detectors.detect_research_backends(
        [{"name": "exa-search", "ok": False, "detail": "HTTP 401 (bad key)"}])
    assert len(out) == 1
    f = out[0]
    assert f.severity == "high"
    assert f.signature == "research-backend-down:exa-search"
    assert f.recommended_owner == "Infrastructure"
    assert f.evidence["backend"] == "exa-search"
    assert "401" in f.evidence["detail"]
    assert "exa-search" in f.title


def test_healthy_backend_produces_nothing():
    assert detectors.detect_research_backends(
        [{"name": "web-read", "ok": True, "detail": ""}]) == []


def test_only_down_backends_are_reported():
    probes = [
        {"name": "web-read", "ok": True},
        {"name": "exa-search", "ok": False, "detail": "timeout"},
        {"name": "video-transcript", "ok": False, "detail": "yt-dlp missing"},
    ]
    out = detectors.detect_research_backends(probes)
    assert {f.evidence["backend"] for f in out} == {"exa-search", "video-transcript"}
    assert all(f.severity == "high" for f in out)


def test_missing_detail_does_not_crash():
    out = detectors.detect_research_backends([{"name": "brave-search", "ok": False}])
    assert len(out) == 1
    assert out[0].evidence["backend"] == "brave-search"


def test_empty_probe_list_is_no_findings():
    assert detectors.detect_research_backends([]) == []
