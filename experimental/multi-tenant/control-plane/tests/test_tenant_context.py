"""Offline contract tests for token-to-RLS tenant context binding."""

from user_tokens import Principal, issue_user_token
from tenant_context import TenantContextError, bind_tenant_context, principal_from_authorization

import pytest


SECRET = "test-secret"
TENANT_ID = "00000000-0000-0000-0000-000000000001"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=()):
        self.conn.calls.append((query, params))
        if self.conn.fail:
            raise RuntimeError("database details must stay internal")

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.rollbacks = 0
        self.fail = False

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1


def token(*, tenant_id=TENANT_ID):
    return issue_user_token(
        user_id="user-1",
        tenant_id=tenant_id,
        role="member",
        secret=SECRET,
        now=100,
    )


def test_authorization_verification_returns_signed_principal():
    principal = principal_from_authorization(f"Bearer {token()}", SECRET, now=101)

    assert principal == Principal("user-1", TENANT_ID, "member")


@pytest.mark.parametrize("authorization", [None, "", "Basic abc", "Bearer "])
def test_missing_bearer_context_fails_closed(authorization):
    with pytest.raises(TenantContextError, match="missing tenant bearer token"):
        principal_from_authorization(authorization, SECRET, now=101)


def test_bad_signature_is_not_exposed_as_a_database_or_token_detail():
    with pytest.raises(TenantContextError, match="invalid tenant context"):
        principal_from_authorization(f"Bearer {token()}", "wrong", now=101)


def test_non_uuid_tenant_id_is_rejected_before_database_access():
    with pytest.raises(TenantContextError, match="invalid tenant context"):
        principal_from_authorization(f"Bearer {token(tenant_id='tenant-1')}", SECRET, now=101)


def test_binding_is_transaction_local_and_parameterized():
    conn = FakeConnection()

    bind_tenant_context(conn, Principal("user-1", TENANT_ID, "member"))

    assert conn.calls == [
        ("SELECT set_config('app.tenant_id', %s, true)", (TENANT_ID,))
    ]
    assert conn.rollbacks == 0


def test_binding_failure_rolls_back_and_hides_database_details():
    conn = FakeConnection()
    conn.fail = True

    with pytest.raises(TenantContextError, match="could not bind tenant context"):
        bind_tenant_context(conn, Principal("user-1", TENANT_ID, "member"))

    assert conn.rollbacks == 1
