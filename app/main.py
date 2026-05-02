"""FastAPI app for the rndexp.art ecosystem dashboard.

Phase 1 routes (this commit):
  GET  /healthz       liveness (unauthenticated)
  GET  /              admin home — links to each section
  GET  /users         user CRUD UI (skeleton; backend wiring in Phase 2)
  GET  /vps           VPS metrics (skeleton; SSH polling in Phase 3)
  GET  /services      service status + logs (skeleton; SSH polling in Phase 3)
  GET  /projects      create-project + pending-merge UI (skeleton; Phase 4)

Authn/Authz: every route except /healthz requires the `admin` role, enforced
by `app.auth.require_admin` (which reads the X-Auth-* headers Caddy injects
via `forward_auth`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import AuthedUser, require_admin
from .auth_client import AuthClient
from . import docker_client, github_client, vps_metrics
from .docker_client import DockerUnavailable


TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


app = FastAPI(title="rndexp-art dashboard", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


def _section(request: Request, me: AuthedUser, name: str, ctx: dict | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        f"{name}.html",
        {"me": me, "active": name, **(ctx or {})},
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, me: Annotated[AuthedUser, Depends(require_admin)]):
    return _section(request, me, "index")


@app.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    me: Annotated[AuthedUser, Depends(require_admin)],
    flash: str = "",
    invite_url: str = "",
    invite_email: str = "",
    invite_email_sent: str = "",
):
    """Users tab — lists every account, admin actions inline.

    `?flash=` and `?invite_url=` are populated by the action handlers below
    when they redirect back here, so the operator sees confirmation +,
    if SMTP is unconfigured, the raw invite/reset link to copy.
    """
    with AuthClient(request) as auth:
        users = auth.list_users()
        roles = auth.list_role_slugs()
    return _section(request, me, "users", {
        "users": users,
        "roles": roles,
        "flash": flash,
        "invite_url": invite_url,
        "invite_email": invite_email,
        "invite_email_sent": invite_email_sent == "1",
    })


def _users_redirect(*, flash: str = "", invite_url: str = "",
                    invite_email: str = "", invite_email_sent: bool | None = None) -> RedirectResponse:
    from urllib.parse import urlencode
    params: dict[str, str] = {}
    if flash:
        params["flash"] = flash
    if invite_url:
        params["invite_url"] = invite_url
    if invite_email:
        params["invite_email"] = invite_email
    if invite_email_sent is not None:
        params["invite_email_sent"] = "1" if invite_email_sent else "0"
    qs = ("?" + urlencode(params)) if params else ""
    return RedirectResponse(url=f"/users{qs}", status_code=303)


def _humanize_http_error(e: HTTPException) -> str:
    return f"{e.status_code}: {e.detail}"


@app.post("/users/invite")
def users_invite(
    request: Request,
    _: Annotated[AuthedUser, Depends(require_admin)],
    email: Annotated[str, Form()],
    roles: Annotated[list[str], Form()] = [],
):
    try:
        with AuthClient(request) as auth:
            result = auth.invite(email=email.strip().lower(), roles=roles)
    except HTTPException as e:
        return _users_redirect(flash=_humanize_http_error(e))
    return _users_redirect(
        flash=f"Invite issued for {result['email']}.",
        invite_url=result["invite_url"] if not result["email_sent"] else "",
        invite_email=result["email"],
        invite_email_sent=result["email_sent"],
    )


@app.post("/users/{user_id}/reset-password")
def users_reset_password(
    user_id: int,
    request: Request,
    _: Annotated[AuthedUser, Depends(require_admin)],
):
    try:
        with AuthClient(request) as auth:
            result = auth.reset_password(user_id)
    except HTTPException as e:
        return _users_redirect(flash=_humanize_http_error(e))
    return _users_redirect(
        flash=f"Reset link issued for {result['email']}.",
        invite_url=result["invite_url"] if not result["email_sent"] else "",
        invite_email=result["email"],
        invite_email_sent=result["email_sent"],
    )


@app.post("/users/{user_id}/delete")
def users_delete(
    user_id: int,
    request: Request,
    _: Annotated[AuthedUser, Depends(require_admin)],
):
    try:
        with AuthClient(request) as auth:
            auth.delete_user(user_id)
    except HTTPException as e:
        return _users_redirect(flash=_humanize_http_error(e))
    return _users_redirect(flash="User deleted.")


@app.post("/users/{user_id}/roles")
def users_grant_role(
    user_id: int,
    request: Request,
    _: Annotated[AuthedUser, Depends(require_admin)],
    role_slug: Annotated[str, Form()],
):
    try:
        with AuthClient(request) as auth:
            auth.grant_role(user_id, role_slug)
    except HTTPException as e:
        return _users_redirect(flash=_humanize_http_error(e))
    return _users_redirect(flash=f"Granted `{role_slug}`.")


@app.post("/users/{user_id}/roles/{role_slug}/revoke")
def users_revoke_role(
    user_id: int,
    role_slug: str,
    request: Request,
    _: Annotated[AuthedUser, Depends(require_admin)],
):
    try:
        with AuthClient(request) as auth:
            auth.revoke_role(user_id, role_slug)
    except HTTPException as e:
        return _users_redirect(flash=_humanize_http_error(e))
    return _users_redirect(flash=f"Revoked `{role_slug}`.")


@app.get("/vps", response_class=HTMLResponse)
def vps_page(request: Request, me: Annotated[AuthedUser, Depends(require_admin)]):
    return _section(request, me, "vps", {
        "host_available": vps_metrics.host_metrics_available(),
        "cpu": vps_metrics.cpu(),
        "memory": vps_metrics.memory(),
        "disk": vps_metrics.disk(),
        "network": vps_metrics.network(),
        "uptime": vps_metrics.humanize_uptime(vps_metrics.uptime_seconds()),
    })


@app.get("/services", response_class=HTMLResponse)
def services_page(request: Request, me: Annotated[AuthedUser, Depends(require_admin)]):
    services: list[docker_client.ContainerInfo] = []
    docker_error: str | None = None
    try:
        services = docker_client.list_services()
    except DockerUnavailable as e:
        docker_error = str(e)

    pins = github_client.submodule_pins(branch="production")
    pins_by_name = {p.path.removeprefix("services/"): p for p in pins}

    runs = github_client.recent_runs("deploy-gateway.yml", per_page=10)

    return _section(request, me, "services", {
        "services": services,
        "docker_error": docker_error,
        "pins_by_name": pins_by_name,
        "runs": runs,
        "gh_configured": github_client.gh_configured(),
        "humanize_uptime": docker_client.humanize_uptime,
    })


@app.get("/services/{name}/logs", response_class=HTMLResponse)
def service_logs(
    name: str,
    request: Request,
    me: Annotated[AuthedUser, Depends(require_admin)],
    tail: int = 200,
):
    """Plain-text log dump for one container. Rendered inline in services.html
    via a <details> element on the service row.
    """
    try:
        text = docker_client.logs(name, tail=tail)
    except DockerUnavailable as e:
        text = f"docker unavailable: {e}"
    # Embed in a minimal page so it can be opened standalone in a tab too.
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8>"
        f"<title>{name} logs · dashboard</title>"
        f"<style>body{{font:12px/1.4 ui-monospace,monospace;margin:16px;white-space:pre-wrap;word-break:break-all}}</style>"
        f"<body>{_html_escape(text) or '(no log lines)'}</body>"
    )


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
              .replace(">", "&gt;").replace("\"", "&quot;"))


@app.get("/services/{name}/stats")
def service_stats(
    name: str,
    me: Annotated[AuthedUser, Depends(require_admin)],
):
    """JSON endpoint for ad-hoc polling later. Used by the row's CPU% cell
    if/when we wire a tiny refresh script. Today the page renders these
    server-side on initial load."""
    try:
        s = docker_client.stats(name)
    except DockerUnavailable as e:
        return {"available": False, "error": str(e)}
    return {
        "available": s.available,
        "cpu_pct": s.cpu_pct,
        "mem_used_mb": s.mem_used_mb,
        "mem_limit_mb": s.mem_limit_mb,
        "mem_pct": s.mem_pct,
    }


@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request, me: Annotated[AuthedUser, Depends(require_admin)]):
    return _section(request, me, "projects")
