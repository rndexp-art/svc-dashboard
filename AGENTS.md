# AGENTS.md — dashboard service

This is a service submodule of the [rndexpart gateway](https://github.com/rndexp-art/rndexpart). Read the gateway's [AGENTS.md](https://github.com/rndexp-art/rndexpart/blob/main/AGENTS.md) for the overall architecture and tooling.

## What this service is
- Public hostname: `dashboard.rndexp.art` (production), `dashboard.rndexp.localhost` (dev).
- Internal port: **8003**.

## What lives here
- `compose.fragment.yml` — service definition (NOT a standalone compose; only valid when included by the gateway).
- `caddy.fragment` — Caddy site block; the gateway concatenates it into the rendered Caddyfile.
- `.env.example` — env vars; values live in the gateway's `.env` (local) or GH Actions Secrets (prod).

## How deploys work
1. Push to `main` for development; merge to `production` to deploy.
2. `.github/workflows/deploy.yml` fires on push to `production`. It calls `repository_dispatch` against the gateway repo with `event_type=service-updated` and `client_payload.service=dashboard`.
3. The gateway's `deploy-gateway.yml` workflow handles the rest: it bumps the submodule pin, SSHes to the VPS, re-renders, and `docker compose up -d`.

## Conventions
- Bind container ports to `127.0.0.1` only. Caddy (host network) is the public ingress.
- All hostnames in `caddy.fragment` use the production form (`*.rndexp.art`); the gateway's renderer rewrites them for local.
- Env vars referenced from `compose.fragment.yml` must be set in the gateway repo's env (so `docker compose` resolves them).

## Adding a tool / agent task
- Don't add a separate Python tooling stack here. Use the gateway's `tools/rndexp` from the parent repo for cross-cutting actions (deploy, restart, secrets).
