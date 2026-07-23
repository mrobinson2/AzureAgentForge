"""RBAC role->scope enforcement — offline tests."""

import asyncio

import pytest
from fastapi import HTTPException

from app.auth import Principal
from app.rbac import (
    ROLE_SCOPES,
    SCOPE_MEMORY_DELETE,
    SCOPE_MEMORY_READ,
    SCOPE_MEMORY_WRITE,
    SCOPE_TENANT_ADMIN,
    require_scope,
    role_grants,
)


def _p(role):
    return Principal(tenant_id="t1", user_id="u1", role=role)


def _admit(scope, principal):
    """Run the require_scope dependency directly with an explicit principal."""
    return asyncio.run(require_scope(scope)(principal=principal))


def test_matrix_is_monotonic():
    # each higher role is a strict superset of the one below
    assert ROLE_SCOPES["viewer"] < ROLE_SCOPES["member"]
    assert ROLE_SCOPES["member"] < ROLE_SCOPES["operator"]
    assert ROLE_SCOPES["operator"] < ROLE_SCOPES["owner"]


def test_role_grants_expected_scopes():
    assert role_grants("viewer", SCOPE_MEMORY_READ)
    assert not role_grants("viewer", SCOPE_MEMORY_WRITE)
    assert role_grants("member", SCOPE_MEMORY_WRITE)
    assert not role_grants("member", SCOPE_MEMORY_DELETE)
    assert role_grants("operator", SCOPE_MEMORY_DELETE)
    assert not role_grants("operator", SCOPE_TENANT_ADMIN)
    assert role_grants("owner", SCOPE_TENANT_ADMIN)


def test_unknown_role_grants_nothing():
    assert not role_grants("superadmin", SCOPE_MEMORY_READ)
    assert not role_grants("", SCOPE_MEMORY_READ)


def test_require_scope_admits_when_granted():
    p = _p("member")
    assert _admit(SCOPE_MEMORY_WRITE, p) is p  # returns the principal through


def test_require_scope_403_when_lacking():
    with pytest.raises(HTTPException) as exc:
        _admit(SCOPE_MEMORY_DELETE, _p("member"))
    assert exc.value.status_code == 403


def test_viewer_cannot_write():
    with pytest.raises(HTTPException) as exc:
        _admit(SCOPE_MEMORY_WRITE, _p("viewer"))
    assert exc.value.status_code == 403


def test_owner_can_manage_users():
    from app.rbac import SCOPE_USER_MANAGE

    p = _p("owner")
    assert _admit(SCOPE_USER_MANAGE, p) is p
