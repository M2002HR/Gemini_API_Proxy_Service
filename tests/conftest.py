from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from api.app.config import get_settings
from api.app.main import app


BASE_TEST_ENV: Dict[str, str] = {
    "APP_CONFIG_FILE": "config/config.yml",
    "PROXY_MODE": "cloudflare_worker",
    "CLOUDFLARE_WORKER_BASE_URLS": "https://worker.example.test",
    "CLOUDFLARE_WORKER_ROUTE_PREFIX": "/gemini",
    "CLOUDFLARE_AUTH_TOKEN": "test-worker-token",
    "ADMIN_ENABLED": "true",
    "ADMIN_REQUIRE_AUTH": "false",
    "ADMIN_TOKEN": "",
    "ADMIN_HEADER_NAME": "x-admin-token",
    "GEMINI_API_KEYS": "test-key-1,test-key-2",
}


@contextmanager
def client_with_env(overrides: Dict[str, str] | None = None) -> Iterator[TestClient]:
    overrides = overrides or {}
    touched = {}

    for key, value in {**BASE_TEST_ENV, **overrides}.items():
        touched[key] = os.environ.get(key)
        os.environ[key] = value

    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        for key, old_val in touched.items():
            if old_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_val
        get_settings.cache_clear()


@pytest.fixture
def client_factory():
    return client_with_env
