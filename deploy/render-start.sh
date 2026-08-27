#!/bin/sh
# shellcheck shell=sh
# Render start script for the Hermes Agent Docker image.
#
# Render runs ONE service with ONE persistent disk mounted at /opt/data
# ($HERMES_HOME). The messaging gateway and the web dashboard must share
# that home, so both live in this single container. Render injects the
# listen port as $PORT and routes traffic (and the health check) to it,
# and Render Blueprints do NOT interpolate variables, so nothing in
# render.yaml can point the image's supervised dashboard service at $PORT.
# This script therefore owns the dashboard process and binds it to $PORT
# directly, while the gateway runs alongside it.
#
# Two runtime shapes are handled, mirroring docker/entrypoint-dispatch.sh:
#
#   * s6-overlay is PID 1 (the image owns init): the boot reconciler
#     (cont-init.d/02-reconcile-profiles) already auto-started the
#     supervised gateway because HERMES_GATEWAY_BOOTSTRAP_STATE=running
#     seeded gateway_state.json on first boot. We only idempotently ensure
#     it is up, then exec the dashboard as the container's main program.
#
#   * The platform wraps the entrypoint (no s6 supervision): we start the
#     gateway in the background ourselves, then exec the dashboard.
#
# The dashboard is exec'd in the foreground so it is the process bound to
# $PORT — Render's health check (/api/health) and all browser traffic hit
# it directly, and if it dies the container exits and Render restarts it.
# Binding 0.0.0.0 engages the dashboard auth gate (fail-closed on
# non-loopback binds); the bundled password provider is configured via the
# HERMES_DASHBOARD_BASIC_AUTH_* env vars set in render.yaml.

set -eu

HERMES_HOME="${HERMES_HOME:-/opt/data}"
PORT="${PORT:-9119}"
# HOME-anchored state must land on the data volume, not /root.
export HOME="$HERMES_HOME"

log() { printf '[render-start] %s\n' "$*" >&2; }

# --- Detect s6-overlay (same signal as hermes_cli.service_manager._s6_running) ---
# Both signals are required: /proc/1/comm == s6-svscan AND /run/s6/basedir.
s6_running=false
if [ -r /proc/1/comm ] && [ -d /run/s6/basedir ]; then
    if [ "$(cat /proc/1/comm 2>/dev/null || true)" = "s6-svscan" ]; then
        s6_running=true
    fi
fi

if [ "$s6_running" = true ]; then
    # Gateway is supervised by s6 and was auto-started by the boot
    # reconciler. `hermes gateway start` dispatches via the s6 service
    # manager and returns immediately; it is a no-op if already running.
    log "s6-overlay detected; ensuring supervised gateway is up"
    hermes gateway start || log "gateway start returned non-zero (likely already running)"
else
    # No s6 supervision (Render wrapped the entrypoint). Run the gateway
    # in the background so the dashboard can be the foreground process on
    # $PORT. --no-supervise is a no-op outside the s6 image but documents
    # intent: we want plain foreground semantics here.
    log "no s6 supervision; starting gateway in the background"
    mkdir -p "$HERMES_HOME/logs"
    nohup hermes gateway run --no-supervise \
        >> "$HERMES_HOME/logs/render-gateway.log" 2>&1 &
    log "gateway pid $! logging to $HERMES_HOME/logs/render-gateway.log"
fi

# --- Seed dashboard.trusted_proxies before the dashboard reads config.yaml ---
# Render's proxy terminates TLS and forwards X-Forwarded-* headers; the
# dashboard only honours them for peers listed in dashboard.trusted_proxies
# (config.yaml-only, no env bridge). Idempotent; degrades without failing.
if [ -x /opt/hermes/.venv/bin/python ]; then
    /opt/hermes/.venv/bin/python /opt/hermes/deploy/render_bootstrap.py \
        || log "trusted_proxies seeding failed (continuing)"
else
    log "venv python not found; skipping trusted_proxies seeding"
fi

# --- Dashboard: the container's HTTP server on Render's $PORT ---
# --no-open: headless container, never try to open a browser.
log "starting dashboard on 0.0.0.0:$PORT"
exec hermes dashboard --host 0.0.0.0 --port "$PORT" --no-open
