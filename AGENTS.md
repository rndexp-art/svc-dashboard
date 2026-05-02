# AGENTS.md — dashboard service

This is a service submodule of the [rndexpart gateway](https://github.com/rndexp-art/rndexpart). Read the gateway's [AGENTS.md](https://github.com/rndexp-art/rndexpart/blob/main/AGENTS.md) first.

## What this service is

A FastAPI + Jinja admin console for the whole `rndexp.art` ecosystem.

- Public hostname: `dashboard.rndexp.art` (production), `dashboard.rndexp.localhost` (dev).
- Internal port: **8003**.
- Gated behind the auth service: every path except `/healthz` requires a valid
  session cookie *and* the `admin` role. The Caddy site block `import`s the
  `rndexp_auth_forward` snippet defined by `services/auth/caddy.fragment`, so
  unauthenticated visitors get bounced to `auth.rndexp.art/` automatically.

## How auth works here

This service does **not** parse cookies, decode JWTs, or talk to the auth DB.
Caddy's `forward_auth` does the verification on every request, and on success
injects four headers into the upstream request:

| Header | Source |
|---|---|
| `X-Auth-Sub` | numeric user id, as a string |
| `X-Auth-Email` | verified email |
| `X-Auth-Roles` | comma-separated role slugs |
| `X-Auth-Permissions` | comma-separated permission slugs |

`app/auth.py:require_admin` is a FastAPI dependency that 401s if the headers
are missing (which would mean Caddy is misconfigured or someone hit the
container directly) and 403s if the `admin` role is absent. It is the only
authorization mechanism in this service — every gated route depends on it.

## What lives here

- `compose.fragment.yml` — service definition, included by the gateway compose.
- `caddy.fragment` — Caddy site block, concatenated into the gateway Caddyfile.
- `Dockerfile` — builds the FastAPI image (uv, Python 3.12 slim).
- `app/` — FastAPI source (`main.py` routes, `auth.py` admin gate, `templates/` Jinja).
- `pyproject.toml` / `requirements.txt` — pinned deps (mirrors the auth service).

## Roadmap (the dashboard is built in phases)

| Phase | What lands |
|---|---|
| 1 (✓) | Service scaffold, admin gate, skeleton UI for all sections, healthz. |
| 2 | Users CRUD: list, invite-by-email, password set on invite click, password reset, role grants/revokes, last-admin delete guard. Backed by new JSON endpoints + email/password support added to `svc-auth`. |
| 3 | VPS metrics (cpu/mem/disk/network) + per-service status, version pin, recent deploys, container logs. Pulled live on each page load via SSH against `contabo-vps`. |
| 4 | "Create project" flow: GitHub repo + `feat/add-X` working branch off `production`, **Merge to production** button, then fast-forward `main`. |

The skeleton templates already document each phase's plan inline.

## Required env vars

None in Phase 1. Phase 2+ will add:

| Var | Purpose | Phase |
|---|---|---|
| `AUTH_INTERNAL_BASE_URL` | Where the dashboard backend reaches `svc-auth` for the new JSON CRUD API. Defaults to `http://auth:8001`. | 2 |
| `GITHUB_PAT` | Same PAT `tools/rndexp` uses; needed for repo creation + PR merges in the projects flow. | 4 |
| `DASHBOARD_VPS_SSH_KEY_PATH` | Path inside the container to the SSH key for the VPS metrics queries. | 3 |

## How deploys work

1. Push to `main` for development; merge to `production` to deploy.
2. `.github/workflows/deploy.yml` fires on push to `production` and dispatches the gateway with `event_type=service-updated`, `client_payload.service=dashboard`.
3. The gateway's `deploy-gateway.yml` bumps the submodule pin, SSHes to the VPS, re-renders, and `docker compose up -d`.

## Conventions

- Containers connect to other services by their compose service name on the
  default project network (e.g. `auth:8001`); container ports are not published.
  Caddy is the only public ingress.
- All hostnames in `caddy.fragment` use the production form (`*.rndexp.art`);
  the gateway's renderer rewrites them for local.
- Don't add a separate tooling stack here. Use the gateway's `tools/rndexp`
  from the parent repo for cross-cutting actions (deploy, restart, secrets).
