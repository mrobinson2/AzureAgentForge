"""Contract tests for the model-router → control-plane budget adapter."""

import asyncio
import json

import httpx
import pytest

from tenant_budget_client import TenantBudgetClient, TenantBudgetUnavailable


def test_reserve_sends_stable_identity_and_returns_pending_decision():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "tenant_id": seen["body"]["tenant_id"],
                "caller_id": seen["body"]["caller_id"],
                "request_id": seen["body"]["request_id"],
                "day": seen["body"]["day"],
                "reserved_usd": seen["body"]["reserved_usd"],
                "actual_usd": None,
                "cap_usd": "1.00",
                "spent_before_usd": "0.10",
                "spent_after_usd": "0.35",
                "decision": "allow",
                "status": "pending",
            },
        )

    client = TenantBudgetClient(
        base_url="https://control-plane.test",
        api_key="operator-key",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        client.reserve(
            tenant_id="00000000-0000-0000-0000-000000000001",
            caller_id="agent-7",
            request_id="router-request-1",
            reserved_usd="0.25",
            day="2026-08-20",
        )
    )

    assert seen == {
        "path": "/internal/budget/reservations",
        "auth": "Bearer operator-key",
        "body": {
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "caller_id": "agent-7",
            "request_id": "router-request-1",
            "reserved_usd": "0.25",
            "day": "2026-08-20",
        },
    }
    assert result.status == "pending"
    assert result.decision == "allow"


def _response_payload(**overrides):
    payload = {
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "caller_id": "agent-7",
        "request_id": "router-request-1",
        "day": "2026-08-20",
        "reserved_usd": "0.25",
        "actual_usd": None,
        "cap_usd": "1.00",
        "spent_before_usd": "0.10",
        "spent_after_usd": "0.35",
        "decision": "allow",
        "status": "pending",
    }
    payload.update(overrides)
    return payload


def test_reserve_rejects_mismatched_authority_identity():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json=_response_payload(
                tenant_id="00000000-0000-0000-0000-000000000099"
            ),
        )
    )
    client = TenantBudgetClient(
        base_url="https://control-plane.test",
        api_key="operator-key",
        transport=transport,
    )

    with pytest.raises(TenantBudgetUnavailable):
        asyncio.run(
            client.reserve(
                tenant_id="00000000-0000-0000-0000-000000000001",
                caller_id="agent-7",
                request_id="router-request-1",
                reserved_usd="0.25",
                day="2026-08-20",
            )
        )


def test_settle_rejects_impossible_lifecycle_state():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=_response_payload(status="pending"))
    )
    client = TenantBudgetClient(
        base_url="https://control-plane.test",
        api_key="operator-key",
        transport=transport,
    )

    with pytest.raises(TenantBudgetUnavailable):
        asyncio.run(
            client.settle(
                tenant_id="00000000-0000-0000-0000-000000000001",
                request_id="router-request-1",
                actual_usd="0.07",
            )
        )


def test_release_rejects_impossible_lifecycle_state():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=_response_payload(status="settled"))
    )
    client = TenantBudgetClient(
        base_url="https://control-plane.test",
        api_key="operator-key",
        transport=transport,
    )

    with pytest.raises(TenantBudgetUnavailable):
        asyncio.run(
            client.release(
                tenant_id="00000000-0000-0000-0000-000000000001",
                request_id="router-request-1",
            )
        )
