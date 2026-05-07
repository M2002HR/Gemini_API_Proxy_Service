from __future__ import annotations

import textwrap

import pytest

from api.app.config import Settings, load_settings


ENV_KEYS_TO_CLEAR = [
    "APP_CONFIG_FILE",
    "PROXY_MODE",
    "PROXY_TRUST_ENV_PROXY",
    "PROXY_RETRY_ON_429",
    "PROXY_MAX_RETRIES_PER_KEY",
    "PROXY_COOLOFF_SEC",
    "PROXY_MIN_INTERVAL_SEC",
    "GEMINI_API_KEYS",
    "GEMINI_DEFAULT_MODEL",
    "CLOUDFLARE_WORKER_BASE_URLS",
    "CLOUDFLARE_WORKER_ROUTE_PREFIX",
    "CLOUDFLARE_AUTH_TOKEN",
    "CLOUDFLARE_ACCESS_CLIENT_ID",
    "CLOUDFLARE_ACCESS_CLIENT_SECRET",
]


def _clear_env(monkeypatch) -> None:
    for key in ENV_KEYS_TO_CLEAR:
        monkeypatch.setenv(key, "")


def test_settings_defaults() -> None:
    s = Settings()
    assert s.proxy.mode in {"cloudflare_worker", "gemini_direct"}
    assert s.gemini.default_model
    assert s.admin.enabled is True
    assert s.admin.max_recent_requests > 0


def test_load_settings_normalizes_worker_url(tmp_path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "cfg.yml"
    cfg.write_text(
        textwrap.dedent(
            """
            proxy:
              mode: cloudflare_worker
            cloudflare:
              worker_base_urls:
                - worker.example.test
            """
        ),
        encoding="utf-8",
    )

    s = load_settings(config_file=str(cfg))
    assert s.cloudflare.worker_base_urls == ["https://worker.example.test"]


def test_env_overrides_config(tmp_path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "cfg.yml"
    cfg.write_text(
        textwrap.dedent(
            """
            app:
              port: 1111
            proxy:
              mode: cloudflare_worker
            cloudflare:
              worker_base_urls:
                - https://from-config.example
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("APP_PORT", "9999")
    monkeypatch.setenv("CLOUDFLARE_WORKER_BASE_URLS", "https://from-env.example")
    s = load_settings(config_file=str(cfg))

    assert s.app.port == 9999
    assert s.cloudflare.worker_base_urls == ["https://from-env.example"]


def test_cloudflare_mode_requires_worker_urls(tmp_path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "cfg.yml"
    cfg.write_text(
        textwrap.dedent(
            """
            proxy:
              mode: cloudflare_worker
            cloudflare:
              worker_base_urls: []
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="worker_base_urls"):
        load_settings(config_file=str(cfg))


def test_cloudflare_placeholder_rejected(tmp_path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "cfg.yml"
    cfg.write_text(
        textwrap.dedent(
            """
            proxy:
              mode: cloudflare_worker
            cloudflare:
              worker_base_urls:
                - https://your-worker-subdomain.workers.dev
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="placeholder URL"):
        load_settings(config_file=str(cfg))


def test_gemini_direct_requires_keys(tmp_path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "cfg.yml"
    cfg.write_text(
        textwrap.dedent(
            """
            proxy:
              mode: gemini_direct
            gemini:
              api_keys: []
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="gemini.api_keys"):
        load_settings(config_file=str(cfg))
