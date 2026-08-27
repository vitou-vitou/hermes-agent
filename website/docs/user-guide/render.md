---
sidebar_position: 8
title: "Deploying to Render"
description: "Run the Hermes gateway and dashboard on Render with a persistent disk"
---

# Deploying to Render

Hermes ships a [Render Blueprint](https://render.com/docs/blueprints) (`render.yaml` at the repo root) that deploys the agent as a **single web service** running both the messaging gateway and the web dashboard, with all state (`HERMES_HOME`) on a Render persistent disk.

The blueprint uses the **published Docker image** (`nousresearch/hermes-agent`) instead of building from source, so deploys pull a prebuilt image rather than running the multi-stage build on Render.

## Requirements

- A **paid Render plan** (Starter or higher). Persistent disks are not available on the free tier.
- A Render account connected to a repo containing `render.yaml` (fork [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) or copy the file into your own repo).

:::note One service, one disk
A Render persistent disk attaches to exactly one service and cannot be shared. The gateway and dashboard must share `HERMES_HOME` (session database, config, skills, secrets), so they run together in one container — the same topology the official Docker image supports via s6 supervision. The disk also forces a single instance (no horizontal scaling) and disables zero-downtime deploys; both are fine for a personal agent.
:::

## Quick start

1. In the Render dashboard: **New → Blueprint**, select the repo containing `render.yaml`, and apply it. Render creates the `hermes-agent` web service with a 10 GB disk mounted at `/opt/data`.
2. After the first deploy, open the service's **Environment** tab and fill in the prompted values (marked `sync: false` in the blueprint):
   - `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` / `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` — dashboard login. The auth gate is **fail-closed** on public binds: the dashboard refuses to start without a registered auth provider.
   - `OPENROUTER_API_KEY` — or the API key for whichever provider you use (see [Configuring Models](./configuring-models.md)).
   - `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USERS` — if you use Telegram (long-polling; no inbound port needed). Add other platform tokens as needed.
3. Trigger a redeploy (or just restart the service). Open `https://<service>.onrender.com` and log in with the dashboard credentials.

## How it works

| Piece | Mechanism |
|---|---|
| Port binding | Render injects the listen port as `$PORT`. Blueprints don't interpolate variables, so `deploy/render-start.sh` (set as the service's `dockerCommand`) spawns the dashboard directly on `$PORT` and runs the gateway alongside it. |
| Gateway supervision | `HERMES_GATEWAY_BOOTSTRAP_STATE=running` seeds the gateway's state on first boot so the image's s6 boot reconciler auto-starts the supervised gateway. First-boot only — a deliberately-stopped gateway stays stopped. |
| Persistent state | The disk is mounted at `/opt/data`, which is `HERMES_HOME` in the image. Sessions, config, skills, and memory survive deploys and restarts. |
| Proxy trust | Render's load balancer terminates TLS and forwards `X-Forwarded-*` headers. `deploy/render_bootstrap.py` merges `HERMES_DASHBOARD_TRUSTED_PROXIES` (default `10.0.0.0/8`, Render's private network) into `dashboard.trusted_proxies` in `config.yaml` on every boot (idempotent), so the dashboard sees the real client IP and `https` scheme. |
| Health check | `healthCheckPath: /api/health` — an auth-exempt liveness endpoint, so Render's probe works before you log in. |

## Updating

The blueprint tracks `nousresearch/hermes-agent:latest` by default. For deterministic deploys, pin a release tag in `render.yaml`:

```yaml
image:
  url: docker.io/nousresearch/hermes-agent:v0.20.6
```

Then redeploy. The image is stateless — all state lives on the disk, so upgrades don't touch your data.

## Optional configuration

Set these as additional env vars on the service:

- `HERMES_DASHBOARD_PUBLIC_URL` — set to your custom domain (e.g. `https://hermes.example.com`) if you add one, so the dashboard resolves its browser-facing URL correctly.
- `HERMES_DASHBOARD_TRUSTED_PROXIES` — override the proxy range if Render's proxy egress differs in your region. Must be a bounded network (`*` and `/0` are rejected).
- Any platform token from the [messaging docs](./messaging/index.md) — Discord, Slack, Signal, etc.

## Troubleshooting

- **Dashboard won't start, logs mention the auth gate** — the basic-auth env vars are missing or empty. Set both `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` and `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` and redeploy.
- **Health check fails** — check the service logs; the dashboard must be up on `$PORT` for Render to route traffic. `/api/health` itself needs no auth.
- **Gateway not connecting to Telegram/Discord** — verify the token env vars are set and the service was redeployed after setting them. Gateway logs are in the Render log view (and in `$HERMES_HOME/logs/gateway.log` on the disk).
- **Wrong client IP or `http` scheme behind the proxy** — confirm `dashboard.trusted_proxies` in `$HERMES_HOME/config.yaml` contains the proxy range; `render_bootstrap.py` logs what it seeded on boot.
