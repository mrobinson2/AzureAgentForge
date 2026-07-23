# Reference design — part of the multi-tenant roadmap. Phase 3 of
# docs/notes/plans/2026-07-22-full-multi-tenant.md: RBAC enforcement.

"""Role-based access control for the tenant data plane.

Phase 2 gave every caller a verified Principal (tenant_id, user_id, role). This
maps role -> allowed scopes and provides `require_scope`, a FastAPI dependency
factory that admits a request only if the caller's role grants the scope.

Fail-closed by construction:
  - An unknown role grants NO scopes (an unrecognized role can do nothing, never
    everything) — the map is consulted with a default of the empty set.
  - `require_scope` composes `require_principal` (phase 2), so it inherits the
    503-on-unconfigured / 401-on-bad-token posture; a role that lacks the scope
    gets 403.

Mirrors the auth-proxy SCOPE_MAP pattern (a request needs a scope; a caller
either has it or is refused). The role->scope table is intentionally small
(YAGNI): grow it when a real need appears, not before.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from .auth import ROLES, Principal, require_principal

# Data-plane scopes. Keep names aligned with the auth-proxy scope vocabulary
# (resource:verb) so operators reason about one model.
SCOPE_MEMORY_READ = "memory:read"
SCOPE_MEMORY_WRITE = "memory:write"
SCOPE_MEMORY_DELETE = "memory:delete"
SCOPE_TENANT_ADMIN = "tenant:admin"
SCOPE_USER_MANAGE = "user:manage"

# role -> the scopes it grants. Monotonic: each higher role is a superset of the
# one below, which keeps the model easy to reason about.
ROLE_SCOPES: dict[str, frozenset[str]] = {
    "viewer": frozenset({SCOPE_MEMORY_READ}),
    "member": frozenset({SCOPE_MEMORY_READ, SCOPE_MEMORY_WRITE}),
    "operator": frozenset(
        {SCOPE_MEMORY_READ, SCOPE_MEMORY_WRITE, SCOPE_MEMORY_DELETE}
    ),
    "owner": frozenset(
        {
            SCOPE_MEMORY_READ,
            SCOPE_MEMORY_WRITE,
            SCOPE_MEMORY_DELETE,
            SCOPE_TENANT_ADMIN,
            SCOPE_USER_MANAGE,
        }
    ),
}

# Contract guard: every declared role must have an entry, so adding a role to
# auth.ROLES without granting it scopes fails loudly here at import, not silently
# at request time with an accidental empty grant.
assert set(ROLES) == set(ROLE_SCOPES), (
    f"ROLE_SCOPES {sorted(ROLE_SCOPES)} out of sync with auth.ROLES {sorted(ROLES)}"
)


def role_grants(role: str, scope: str) -> bool:
    """True iff `role` grants `scope`. Unknown role -> False (empty grant)."""
    return scope in ROLE_SCOPES.get(role, frozenset())


def require_scope(scope: str):
    """Build a dependency that admits the request only if the caller's role
    grants `scope`, else 403. Composes the phase-2 principal verification."""

    async def _dep(principal: Principal = Depends(require_principal)) -> Principal:
        if not role_grants(principal.role, scope):
            raise HTTPException(
                status_code=403,
                detail=f"role '{principal.role}' lacks required scope '{scope}'",
            )
        return principal

    return _dep
