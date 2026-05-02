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

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .auth import AuthedUser, require_admin


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
def users_page(request: Request, me: Annotated[AuthedUser, Depends(require_admin)]):
    return _section(request, me, "users")


@app.get("/vps", response_class=HTMLResponse)
def vps_page(request: Request, me: Annotated[AuthedUser, Depends(require_admin)]):
    return _section(request, me, "vps")


@app.get("/services", response_class=HTMLResponse)
def services_page(request: Request, me: Annotated[AuthedUser, Depends(require_admin)]):
    return _section(request, me, "services")


@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request, me: Annotated[AuthedUser, Depends(require_admin)]):
    return _section(request, me, "projects")
