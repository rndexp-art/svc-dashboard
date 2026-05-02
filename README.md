# dashboard service

A submodule of [rndexp-art/rndexpart](https://github.com/rndexp-art/rndexpart) — admin console at `dashboard.rndexp.art` (production) / `dashboard.rndexp.localhost` (dev).

FastAPI + Jinja, gated behind [svc-auth](https://github.com/rndexp-art/svc-auth) at the Caddy layer. See [AGENTS.md](AGENTS.md) for the design.

## Files
- `app/` — FastAPI app (routes, admin gate, Jinja templates).
- `Dockerfile` — Python 3.12 slim + uv.
- `compose.fragment.yml` — included by the gateway's compose when this service is enabled.
- `caddy.fragment` — concatenated into the gateway's Caddyfile; gates the site behind `forward_auth`.

## Local dev
This service runs as part of the gateway. From the gateway repo root:

```sh
tools/rndexp service enable dashboard --env local
tools/rndexp up
```

Then visit `https://dashboard.rndexp.localhost` — Caddy will bounce you to the auth login page; sign in with the email registered as `AUTH_INITIAL_ADMIN_EMAIL` and you'll land on the dashboard.

## Tests
```sh
pytest -q   # auth gate + skeleton routes
```

## Deploy
Push to `production` — the workflow in `.github/workflows/deploy.yml` dispatches the gateway, which redeploys with the latest submodule SHA.

```sh
# from this submodule's directory
git push origin main:production
```

## Internal port
Listens on **8003**. Caddy reverse-proxies `dashboard:8003` over the project's docker network.
