"""Admin gate.

The Caddy site for `dashboard.rndexp.art` imports the `rndexp_auth_forward`
snippet defined by the auth service. That snippet calls `auth:8001/verify`
and, on 200, injects four headers into the upstream request:

    X-Auth-Sub          numeric user id, as a string
    X-Auth-Email        verified email
    X-Auth-Roles        comma-separated role slugs
    X-Auth-Permissions  comma-separated permission slugs

On 401 the snippet 302s to the login page, so the dashboard normally only
ever sees authenticated requests. We still re-check the role here as defense
in depth: if the gateway is misconfigured (or the dashboard is reached by a
direct container hit), missing the `admin` role MUST 403.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status


ADMIN_ROLE = "admin"


@dataclass(frozen=True)
class AuthedUser:
    sub: str
    email: str
    roles: list[str]
    permissions: list[str]


def _split_csv(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


def _from_headers(request: Request) -> AuthedUser | None:
    email = request.headers.get("x-auth-email", "").strip().lower()
    sub = request.headers.get("x-auth-sub", "").strip()
    if not email or not sub:
        return None
    return AuthedUser(
        sub=sub,
        email=email,
        roles=_split_csv(request.headers.get("x-auth-roles", "")),
        permissions=_split_csv(request.headers.get("x-auth-permissions", "")),
    )


def require_admin(request: Request) -> AuthedUser:
    """FastAPI dependency: 401 if no auth headers, 403 if not an admin."""
    user = _from_headers(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="auth headers missing — request did not pass forward_auth",
        )
    if ADMIN_ROLE not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return user
