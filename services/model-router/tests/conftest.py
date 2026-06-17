"""Shared fixtures for the router tests: the imported app module and a
FastAPI TestClient over it. No network — handlers are monkeypatched per test.

Env priming happens at module import time (collection), *before* any test
module imports `main`. `main` registers its tiers from env vars at import
time, so the baseline `gpt4o-mini` and `phi4` tiers must be primed here —
otherwise a test file collected alphabetically before `test_routing.py`
could import `main` first with no env set, registering an empty MODELS table
and breaking every tier-presence assertion. Priming in conftest (always
imported before the test modules in its directory) makes collection order
irrelevant. `test_routing.py` keeps its own `setdefault` calls; they become
idempotent no-ops once these run first.

`main` itself is imported lazily inside the fixtures (not at collection time)
so the env vars below are already in place when the first import fires."""

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Prime both baseline tiers with sentinel creds before `main` is imported.
# setdefault so a real environment (or test_routing's own setdefault) wins.
os.environ.setdefault("GPT4O_BASE_URL", "http://localhost:8888")
os.environ.setdefault("GPT4O_API_KEY", "test-key")
os.environ.setdefault("PHI_BASE_URL", "http://localhost:9999")
os.environ.setdefault("PHI_API_KEY", "test-phi-key")


@pytest.fixture
def router():
    """The imported router app module (for monkeypatching its module globals)."""
    import main

    return main


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    import main

    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _isolate_router_state():
    """Snapshot and restore the router's mutable module-level state around each
    test. Several tested functions mutate process globals — record_cost() grows
    _spend, _register_foundry_tier() writes MODELS, the rate limiter grows
    _rate_windows, select_tier() registers passthrough tiers. Without isolation
    these leak across tests and make ordering significant. monkeypatch.setattr
    already restores rebound names; this covers in-place mutation of the shared
    containers."""
    import main

    models_snapshot = {k: dict(v) for k, v in main.MODELS.items()}
    fallback_snapshot = {k: list(v) for k, v in main._FALLBACK_PREFERENCE.items()}
    personas_snapshot = dict(main.PERSONA_TIERS)
    spend_snapshot = dict(main._spend)
    budget_date_snapshot = main._budget_date

    yield

    main.MODELS.clear()
    main.MODELS.update(models_snapshot)
    main._FALLBACK_PREFERENCE.clear()
    main._FALLBACK_PREFERENCE.update(fallback_snapshot)
    main.PERSONA_TIERS.clear()
    main.PERSONA_TIERS.update(personas_snapshot)
    main._spend.clear()
    main._spend.update(spend_snapshot)
    main._budget_date = budget_date_snapshot
    main._rate_windows.clear()


class FakeRequest:
    """Minimal stand-in for starlette.Request for unit-testing the auth and
    rate-limit guards without spinning up the ASGI stack. Only the attributes
    those functions touch are implemented: `.headers.get(...)` and
    `.client.host`."""

    def __init__(self, headers: dict | None = None, host: str = "203.0.113.7"):
        self.headers = headers or {}
        self.client = type("Client", (), {"host": host})() if host is not None else None


@pytest.fixture
def make_request():
    """Factory for FakeRequest instances."""

    def _factory(headers: dict | None = None, host: str = "203.0.113.7"):
        return FakeRequest(headers=headers, host=host)

    return _factory
