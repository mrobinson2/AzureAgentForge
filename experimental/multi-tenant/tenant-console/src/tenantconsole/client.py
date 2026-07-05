"""Thin PaperClip / governor API client for the tenant executor.

Every call goes through the control-plane auth-proxy: Bearer JWT +
``Origin`` matching the base URL + JSON. PaperClip payloads are **camelCase
only** — its schema validation silently strips snake_case. Governor memory
endpoints, by contrast, are snake_case (they match the classifier's field
vocabulary); the executor sends each exactly as the receiving service expects.

Transport is injectable: ``transport(method, path, params, json_body)`` returns
``(status, body)``. The default uses ``requests``; tests pass an in-memory fake
so the whole suite runs offline. Methods are thin — they build the payload,
call the transport, and either return the parsed body or raise
:class:`ApiError`. Lookup helpers return ``None`` on 404 instead of raising, so
"find or create" steps stay branch-legible.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple, Union

__all__ = ["ApiClient", "ApiError", "Transport"]

Body = Union[dict, list, str, None]
#: ``(method, path, params, json_body) -> (status, body)``.
Transport = Callable[[str, str, Optional[dict], Optional[dict]], Tuple[int, Body]]

DEFAULT_TIMEOUT_S = 30

#: The dev stack sits behind Cloudflare, which 403s (error 1010) on non-browser
#: User-Agents like ``python-requests/*``. A browser UA reaches the tunnel — the
#: same class of fix any non-browser fetch needs against the tunnel.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


def _default_headers(token: str, origin: str) -> Dict[str, str]:
    """Headers every live request carries (Bearer + Origin CSRF + browser UA)."""
    return {
        "Authorization": f"Bearer {token}",
        "Origin": origin,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": BROWSER_USER_AGENT,
    }


class ApiError(Exception):
    """A non-2xx response the caller did not opt to inspect."""

    def __init__(self, status: int, detail: Any, method: str = "", path: str = ""):
        self.status = status
        self.detail = detail
        self.method = method
        self.path = path
        where = f" ({method} {path})" if path else ""
        super().__init__(f"API {status}{where}: {detail}")


def _requests_transport(base_url: str, token: str) -> Transport:
    """Real transport over ``requests`` (imported lazily so dry-run stays offline)."""
    import requests  # noqa: WPS433 — deferred so importing the client needs no requests

    origin = base_url.rstrip("/")
    session = requests.Session()
    session.headers.update(_default_headers(token, origin))

    def _transport(
        method: str, path: str, params: Optional[dict], json_body: Optional[dict]
    ) -> Tuple[int, Body]:
        resp = session.request(
            method,
            f"{origin}{path}",
            params=params or None,
            json=json_body,
            timeout=DEFAULT_TIMEOUT_S,
        )
        try:
            body: Body = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, body

    return _transport


class ApiClient:
    """Typed wrapper over the auth-proxy endpoints the executor touches."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: Optional[Transport] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.transport: Transport = transport or _requests_transport(self.base_url, token)

    # -- low-level ---------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Tuple[int, Body]:
        return self.transport(method, path, params, json_body)

    def _require(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Body:
        status, body = self._call(method, path, params, json_body)
        if status >= 400:
            raise ApiError(status, body, method, path)
        return body

    # -- auth --------------------------------------------------------------

    def whoami(self) -> Body:
        return self._require("GET", "/api/auth/whoami")

    # -- companies ---------------------------------------------------------

    def list_companies(self) -> List[dict]:
        body = self._require("GET", "/api/companies")
        return body if isinstance(body, list) else []

    def create_company(
        self,
        name: str,
        description: Optional[str] = None,
        budget_monthly_cents: Optional[int] = None,
    ) -> dict:
        payload: Dict[str, Any] = {"name": name}
        if description is not None:
            payload["description"] = description
        if budget_monthly_cents is not None:
            payload["budgetMonthlyCents"] = budget_monthly_cents
        return self._require("POST", "/api/companies", json_body=payload)  # type: ignore[return-value]

    def patch_company(self, company_id: str, **fields: Any) -> dict:
        return self._require(  # type: ignore[return-value]
            "PATCH", f"/api/companies/{company_id}", json_body=dict(fields)
        )

    # -- agents ------------------------------------------------------------

    def list_agents(self, company_id: str) -> List[dict]:
        body = self._require("GET", f"/api/companies/{company_id}/agents")
        return body if isinstance(body, list) else []

    def create_agent(self, company_id: str, payload: dict) -> dict:
        return self._require(  # type: ignore[return-value]
            "POST", f"/api/companies/{company_id}/agents", json_body=payload
        )

    def patch_agent(self, agent_id: str, payload: dict) -> dict:
        return self._require(  # type: ignore[return-value]
            "PATCH", f"/api/agents/{agent_id}", json_body=payload
        )

    def terminate_agent(self, agent_id: str) -> Body:
        return self._require("POST", f"/api/agents/{agent_id}/terminate")

    def delete_agent(self, agent_id: str) -> Body:
        return self._require("DELETE", f"/api/agents/{agent_id}")

    # -- instructions bundle ----------------------------------------------

    def put_instructions_file(self, agent_id: str, path: str, content: str) -> dict:
        return self._require(  # type: ignore[return-value]
            "PUT",
            f"/api/agents/{agent_id}/instructions-bundle/file",
            json_body={"path": path, "content": content},
        )

    def set_managed_bundle(self, agent_id: str, entry_file: str = "AGENTS.md") -> Body:
        return self._require(
            "PATCH",
            f"/api/agents/{agent_id}/instructions-bundle",
            json_body={"mode": "managed", "entryFile": entry_file},
        )

    def get_instructions_file(self, agent_id: str, path: str) -> Optional[dict]:
        status, body = self._call(
            "GET",
            f"/api/agents/{agent_id}/instructions-bundle/file",
            params={"path": path},
        )
        if status == 404:
            return None
        if status >= 400:
            raise ApiError(status, body, "GET", f"/api/agents/{agent_id}/instructions-bundle/file")
        return body if isinstance(body, dict) else None

    # -- memory (governor via auth-proxy; snake_case) ----------------------

    def admit_memory(self, payload: dict) -> Body:
        return self._require("POST", "/api/memory/admit", json_body=payload)

    def list_memory(
        self,
        workspace_name: str,
        created_by: Optional[str] = "operator",
        limit: int = 200,
    ) -> List[dict]:
        params: Dict[str, Any] = {"workspace_name": workspace_name, "limit": limit}
        if created_by is not None:
            params["created_by"] = created_by
        body = self._require("GET", "/api/memory", params=params)
        return body if isinstance(body, list) else []

    def workspace_expire(self, workspace_name: str) -> dict:
        return self._require(  # type: ignore[return-value]
            "POST",
            "/api/memory/workspace-expire",
            json_body={"workspace_name": workspace_name},
        )

    # -- issues ------------------------------------------------------------

    def list_company_issues(self, company_id: str) -> List[dict]:
        body = self._require("GET", f"/api/companies/{company_id}/issues")
        return body if isinstance(body, list) else []

    def create_issue(
        self,
        company_id: str,
        title: str,
        description: str,
        assignee_agent_id: str,
        status: str = "todo",
    ) -> dict:
        payload = {
            "title": title,
            "description": description,
            "assigneeAgentId": assignee_agent_id,
            "status": status,
        }
        return self._require(  # type: ignore[return-value]
            "POST", f"/api/companies/{company_id}/issues", json_body=payload
        )

    def get_issue(self, identifier_or_id: str) -> Optional[dict]:
        status, body = self._call("GET", f"/api/issues/{identifier_or_id}")
        if status == 404:
            return None
        if status >= 400:
            raise ApiError(status, body, "GET", f"/api/issues/{identifier_or_id}")
        return body if isinstance(body, dict) else None

    def list_issue_comments(self, issue_id: str) -> List[dict]:
        body = self._require("GET", f"/api/issues/{issue_id}/comments")
        return body if isinstance(body, list) else []

    def create_issue_comment(self, issue_id: str, body_text: str) -> dict:
        return self._require(  # type: ignore[return-value]
            "POST", f"/api/issues/{issue_id}/comments", json_body={"body": body_text}
        )

    def patch_issue(self, issue_id: str, status: str) -> dict:
        return self._require(  # type: ignore[return-value]
            "PATCH", f"/api/issues/{issue_id}", json_body={"status": status}
        )
