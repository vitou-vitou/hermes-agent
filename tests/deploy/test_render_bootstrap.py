"""Tests for the Render deployment helpers.

Covers:
* ``deploy/render_bootstrap.py`` — the idempotent
  ``dashboard.trusted_proxies`` seeder (parse, seed, idempotence,
  preservation of unrelated config, corrupt-config degradation).
* ``deploy/render-start.sh`` — POSIX shell syntax check (``sh -n``),
  skipped where no ``sh`` is available (native Windows without a POSIX
  shell on PATH).
* ``render.yaml`` — parses as YAML and carries the load-bearing blueprint
  contract (single web service, image runtime, disk at /opt/data,
  auth-exempt health check, dockerCommand pointing at render-start.sh).
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "deploy" / "render_bootstrap.py"
START_SH = REPO_ROOT / "deploy" / "render-start.sh"
RENDER_YAML = REPO_ROOT / "render.yaml"


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "render_bootstrap", BOOTSTRAP
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def bootstrap():
    return _load_bootstrap()


# ---------------------------------------------------------------------------
# parse_proxies
# ---------------------------------------------------------------------------


def test_parse_proxies_comma_and_whitespace(bootstrap):
    assert bootstrap.parse_proxies("10.0.0.0/8, 172.16.0.0/12") == [
        "10.0.0.0/8",
        "172.16.0.0/12",
    ]
    assert bootstrap.parse_proxies(" 10.0.0.0/8 \t 192.168.0.0/16 ") == [
        "10.0.0.0/8",
        "192.168.0.0/16",
    ]
    assert bootstrap.parse_proxies(",, ,") == []
    assert bootstrap.parse_proxies("") == []


# ---------------------------------------------------------------------------
# seed_trusted_proxies
# ---------------------------------------------------------------------------


def _read_yaml(path: Path):
    from ruamel.yaml import YAML

    yaml = YAML()
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh)


def test_seed_creates_config_when_missing(bootstrap, tmp_path):
    assert bootstrap.seed_trusted_proxies(tmp_path, ["10.0.0.0/8"]) is True
    data = _read_yaml(tmp_path / "config.yaml")
    assert data["dashboard"]["trusted_proxies"] == ["10.0.0.0/8"]


def test_seed_is_idempotent(bootstrap, tmp_path):
    assert bootstrap.seed_trusted_proxies(tmp_path, ["10.0.0.0/8"]) is True
    first = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    # Second run: nothing to add, file untouched.
    assert bootstrap.seed_trusted_proxies(tmp_path, ["10.0.0.0/8"]) is False
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == first


def test_seed_merges_with_existing_entries(bootstrap, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "dashboard:\n  trusted_proxies:\n    - 192.168.1.0/24\n",
        encoding="utf-8",
    )
    assert bootstrap.seed_trusted_proxies(tmp_path, ["10.0.0.0/8"]) is True
    data = _read_yaml(tmp_path / "config.yaml")
    assert data["dashboard"]["trusted_proxies"] == [
        "192.168.1.0/24",
        "10.0.0.0/8",
    ]
    # Already-present entry alone is a no-op.
    assert bootstrap.seed_trusted_proxies(tmp_path, ["192.168.1.0/24"]) is False


def test_seed_preserves_unrelated_keys_and_comments(bootstrap, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "# top comment\n"
        "model:\n"
        "  default: some-model  # keep me\n"
        "gateway:\n"
        "  platforms: [telegram]\n",
        encoding="utf-8",
    )
    assert bootstrap.seed_trusted_proxies(tmp_path, ["10.0.0.0/8"]) is True
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    data = _read_yaml(tmp_path / "config.yaml")
    assert data["model"]["default"] == "some-model"
    assert data["gateway"]["platforms"] == ["telegram"]
    assert "# top comment" in text
    assert "# keep me" in text


def test_seed_handles_scalar_existing_value(bootstrap, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "dashboard:\n  trusted_proxies: 192.168.1.0/24\n", encoding="utf-8"
    )
    assert bootstrap.seed_trusted_proxies(tmp_path, ["10.0.0.0/8"]) is True
    data = _read_yaml(tmp_path / "config.yaml")
    assert data["dashboard"]["trusted_proxies"] == [
        "192.168.1.0/24",
        "10.0.0.0/8",
    ]


def test_seed_degrades_on_corrupt_config(bootstrap, tmp_path):
    original = "dashboard: [unclosed\n"
    (tmp_path / "config.yaml").write_text(original, encoding="utf-8")
    assert bootstrap.seed_trusted_proxies(tmp_path, ["10.0.0.0/8"]) is False
    # Corrupt file left untouched, no temp litter.
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == original
    assert not (tmp_path / "config.yaml.render-tmp").exists()


def test_seed_leaves_no_temp_file_on_success(bootstrap, tmp_path):
    assert bootstrap.seed_trusted_proxies(tmp_path, ["10.0.0.0/8"]) is True
    assert not (tmp_path / "config.yaml.render-tmp").exists()


# ---------------------------------------------------------------------------
# main() env handling
# ---------------------------------------------------------------------------


def test_main_uses_hermes_home_and_env_override(bootstrap, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(
        bootstrap.ENV_VAR, "10.0.0.0/8, 172.16.0.0/12"
    )
    assert bootstrap.main() == 0
    data = _read_yaml(tmp_path / "config.yaml")
    assert data["dashboard"]["trusted_proxies"] == [
        "10.0.0.0/8",
        "172.16.0.0/12",
    ]


def test_main_defaults_to_render_proxy_range(bootstrap, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(bootstrap.ENV_VAR, raising=False)
    assert bootstrap.main() == 0
    data = _read_yaml(tmp_path / "config.yaml")
    assert data["dashboard"]["trusted_proxies"] == [
        bootstrap.DEFAULT_TRUSTED_PROXIES
    ]


def test_main_empty_env_var_is_a_noop(bootstrap, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(bootstrap.ENV_VAR, "  ,  ")
    assert bootstrap.main() == 0
    assert not (tmp_path / "config.yaml").exists()


# ---------------------------------------------------------------------------
# render-start.sh syntax
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("sh") is None, reason="no POSIX sh on PATH"
)
def test_render_start_sh_syntax():
    result = subprocess.run(
        ["sh", "-n", str(START_SH)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# render.yaml blueprint contract
# ---------------------------------------------------------------------------


def test_render_yaml_blueprint_contract():
    from ruamel.yaml import YAML

    yaml = YAML()
    with open(RENDER_YAML, encoding="utf-8") as fh:
        blueprint = yaml.load(fh)

    services = blueprint["services"]
    assert len(services) == 1, (
        "Render disks attach to a single service; gateway + dashboard must "
        "share one HERMES_HOME volume"
    )
    svc = services[0]
    assert svc["type"] == "web"
    assert svc["runtime"] == "image"
    assert "nousresearch/hermes-agent" in svc["image"]["url"]

    # Health check must target an auth-exempt endpoint or Render's probe
    # gets a 401 and the deploy never goes live.
    from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

    assert svc["healthCheckPath"] in PUBLIC_API_PATHS

    # The start script owns the dashboard on Render's $PORT.
    assert "deploy/render-start.sh" in svc["dockerCommand"]

    # Persistent disk mounted where the image expects HERMES_HOME.
    assert svc["disk"]["mountPath"] == "/opt/data"

    # First-boot gateway auto-start must be declared.
    env = {e["key"]: e for e in svc["envVars"]}
    assert env["HERMES_GATEWAY_BOOTSTRAP_STATE"]["value"] == "running"

    # Secrets are prompted, never hardcoded in the blueprint.
    for key in (
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
        "OPENROUTER_API_KEY",
        "TELEGRAM_BOT_TOKEN",
    ):
        assert env[key].get("sync") is False, f"{key} must be sync: false"
