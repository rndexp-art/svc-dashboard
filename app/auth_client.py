"""Thin httpx wrapper around the auth service's JSON API.

Every method takes a Request because we forward the inbound `rndexp_auth`
cookie verbatim — that's how the auth service authorizes the call. No
service-to-service tokens; the admin's own session is the credential.

The base URL is configurable via AUTH_INTERNAL_BASE_URL (default
http://auth:8001 — same docker network as the auth service).
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import HTTPException, Request


_DEFAULT_TIMEOUT = httpx.Timeout(connect=2.0, read=10.0, write=5.0, pool=2.0)


def _base_url() -> str:
    return os.environ.get("AUTH_INTERNAL_BASE_URL", "http://auth:8001").rstrip("/")


def _cookie_name() -> str:
    return os.environ.get("AUTH_COOKIE_NAME", "rndexp_auth")


def _cookies(request: Request) -> dict[str, str]:
    name = _cookie_name()
    tok = request.cookies.get(name)
    if not tok:
        # In production the dashboard is behind forward_auth so this should
        # never happen. If it does, surface a clear 401.
        raise HTTPException(401, "no rndexp_auth cookie on the inbound request")
    return {name: tok}


class AuthClient:
    def __init__(self, request: Request, *, client: httpx.Client | None = None) -> None:
        self._request = request
        self._owns = client is None
        self._client = client or httpx.Client(
            base_url=_base_url(),
            timeout=_DEFAULT_TIMEOUT,
        )

    def __enter__(self) -> "AuthClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._owns:
            self._client.close()

    # --- low-level ---------------------------------------------------------

    def _request_method(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("cookies", _cookies(self._request))
        try:
            r = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"auth service unreachable: {e}")
        return r

    def _json_or_raise(self, r: httpx.Response) -> Any:
        if r.status_code >= 400:
            # Bubble up the auth service's status + message so the dashboard
            # UI can show "cannot delete the last admin" verbatim.
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise HTTPException(r.status_code, str(detail))
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    # --- typed methods -----------------------------------------------------

    def list_users(self) -> list[dict[str, Any]]:
        return self._json_or_raise(self._request_method("GET", "/api/users"))

    def list_role_slugs(self) -> list[str]:
        return self._json_or_raise(self._request_method("GET", "/api/roles"))

    def invite(self, email: str, roles: list[str]) -> dict[str, Any]:
        return self._json_or_raise(self._request_method(
            "POST", "/api/users/invite", json={"email": email, "roles": roles},
        ))

    def reset_password(self, user_id: int) -> dict[str, Any]:
        return self._json_or_raise(self._request_method(
            "POST", f"/api/users/{user_id}/reset-password",
        ))

    def delete_user(self, user_id: int) -> None:
        self._json_or_raise(self._request_method("DELETE", f"/api/users/{user_id}"))

    def grant_role(self, user_id: int, role_slug: str) -> dict[str, Any]:
        return self._json_or_raise(self._request_method(
            "POST", f"/api/users/{user_id}/roles", json={"role_slug": role_slug},
        ))

    def revoke_role(self, user_id: int, role_slug: str) -> dict[str, Any]:
        return self._json_or_raise(self._request_method(
            "DELETE", f"/api/users/{user_id}/roles/{role_slug}",
        ))
