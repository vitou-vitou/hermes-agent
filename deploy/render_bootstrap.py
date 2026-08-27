#!/usr/bin/env python3
"""First-boot config seeder for Render deployments.

Render's load balancer terminates TLS and forwards plain HTTP to the
container, setting ``X-Forwarded-Proto`` / ``X-Forwarded-For``. The
dashboard's uvicorn only honours those headers when the immediate peer is
in ``dashboard.trusted_proxies`` (config.yaml-only; there is no env bridge
— see ``hermes_cli/web_server.py::_dashboard_forwarded_allow_ips``). The
default is loopback-only, so behind Render's proxy the dashboard would see
``http`` and the wrong client IP, breaking Secure cookies and the auth
gate. This seeder writes the Render proxy range into config.yaml.

Idempotent: safe to run on every boot (``deploy/render-start.sh`` invokes
it before starting the dashboard) and as a one-shot Render
``initialDeployHook``. It only touches ``dashboard.trusted_proxies`` and
preserves every other key and comment via ruamel round-trip loading.

The proxy range is read from ``HERMES_DASHBOARD_TRUSTED_PROXIES``
(comma- or whitespace-separated) and defaults to ``10.0.0.0/8`` — Render's
private network range. It is a *bounded* network on purpose: the dashboard
rejects unbounded entries (``*`` or a ``/0``) fail-closed. Adjust the env
var if Render's proxy egress range differs in your region.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_TRUSTED_PROXIES = "10.0.0.0/8"
ENV_VAR = "HERMES_DASHBOARD_TRUSTED_PROXIES"


def parse_proxies(raw: str) -> list[str]:
    """Split a comma/whitespace-separated proxy list, dropping empties."""
    entries = []
    for chunk in raw.replace(",", " ").split():
        entry = chunk.strip()
        if entry:
            entries.append(entry)
    return entries


def seed_trusted_proxies(hermes_home: Path, proxies: list[str]) -> bool:
    """Merge ``proxies`` into ``dashboard.trusted_proxies`` in config.yaml.

    Returns True if config.yaml was written (changed or created), False if
    every entry was already present (no-op). Preserves existing entries and
    all other keys/comments.
    """
    from ruamel.yaml import YAML

    config_path = hermes_home / "config.yaml"
    yaml = YAML()
    yaml.preserve_quotes = True

    data = None
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as fh:
                data = yaml.load(fh)
        except Exception as exc:  # noqa: BLE001 - degrade, don't brick boot
            print(
                f"[render-bootstrap] could not parse {config_path}: {exc}; "
                "leaving it untouched",
                file=sys.stderr,
            )
            return False
    if data is None:
        data = {}
    if not isinstance(data, dict):
        print(
            f"[render-bootstrap] {config_path} is not a mapping; leaving it "
            "untouched",
            file=sys.stderr,
        )
        return False

    dashboard = data.get("dashboard")
    if not isinstance(dashboard, dict):
        dashboard = {}
        data["dashboard"] = dashboard

    existing = dashboard.get("trusted_proxies")
    if existing is None:
        existing = []
    elif isinstance(existing, str):
        existing = [existing]
    elif not isinstance(existing, list):
        existing = []
    existing = [str(e) for e in existing]

    merged = list(existing)
    added = []
    for entry in proxies:
        if entry not in merged:
            merged.append(entry)
            added.append(entry)

    if not added and config_path.exists():
        return False  # already seeded — idempotent no-op

    dashboard["trusted_proxies"] = merged

    # Atomic write: dump to a temp file in the same directory, then rename,
    # so a crash mid-write can't leave a truncated config.yaml.
    tmp_path = config_path.with_name(config_path.name + ".render-tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
        os.replace(tmp_path, config_path)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[render-bootstrap] failed to write {config_path}: {exc}",
            file=sys.stderr,
        )
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return False

    if added:
        print(
            "[render-bootstrap] dashboard.trusted_proxies += "
            + ", ".join(added)
        )
    else:
        print("[render-bootstrap] created config.yaml with trusted_proxies")
    return True


def main() -> int:
    hermes_home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    raw = os.environ.get(ENV_VAR, "").strip() or DEFAULT_TRUSTED_PROXIES
    proxies = parse_proxies(raw)
    if not proxies:
        print(
            f"[render-bootstrap] {ENV_VAR} resolved to an empty list; "
            "nothing to seed",
            file=sys.stderr,
        )
        return 0
    try:
        seed_trusted_proxies(hermes_home, proxies)
    except Exception as exc:  # noqa: BLE001 - never block container start
        print(f"[render-bootstrap] seeding failed: {exc}", file=sys.stderr)
        # Degrade, don't fail the boot: the dashboard still starts, just
        # without the proxy trust entry (operator can add it manually).
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
