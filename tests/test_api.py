from __future__ import annotations

from typing import Any, Dict

class DummyResp:
    def __init__(self, status_code: int = 200, payload: Dict[str, Any] | None = None):
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self._json = payload if payload is not None else {"ok": True}
        self.content = b'{"ok": true}'

    def json(self):
        return self._json


async def _fake_proxy_call(*args, **kwargs):
    return DummyResp(200, {"ok": True, "via": "fake_proxy"})


async def _fake_models(*args, **kwargs):
    return {
        "capability": "all",
        "include_preview": True,
        "from_cache": True,
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "source_key_mask": "AIzaSy...1234",
        "total": 1,
        "models": [
            {
                "name": "models/gemini-2.5-flash",
                "display_name": "Gemini 2.5 Flash",
                "description": "x",
                "input_token_limit": 1,
                "output_token_limit": 1,
                "supported_generation_methods": ["generateContent", "countTokens"],
                "is_preview": False,
            }
        ],
    }


async def _fake_models_summary(*args, **kwargs):
    return {
        "from_cache": True,
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "total_models": 1,
        "generate_models": 1,
        "embed_models": 0,
        "live_models": 0,
        "preview_models": 0,
        "stable_models": 1,
    }


async def _fake_models_refresh(*args, **kwargs):
    return {"ok": True, "fetched_at": "2026-01-01T00:00:00+00:00", "total": 1, "source_key_mask": "AI..."}


async def _fake_keys_check(*args, **kwargs):
    return {
        "ok": True,
        "last_checked_at": "2026-01-01T00:00:00+00:00",
        "items": [
            {
                "slot": 1,
                "key_mask": "AIzaSy...1234",
                "models_access_ok": True,
                "models_status": 200,
                "models_latency_ms": 10.0,
                "models_error": "",
                "generate_test_ok": True,
                "generate_status": 200,
                "generate_latency_ms": 12.0,
                "generate_error": "",
            }
        ],
    }


async def _fake_worker_connectivity(*args, **kwargs):
    return {
        "proxy_mode": "cloudflare_worker",
        "worker_endpoint": "https://worker.example.test/gemini/v1beta/models/gemini-2.5-flash:generateContent",
        "reachable": True,
        "status_code": 200,
        "classification": "ok",
        "location": "",
        "detail": "ok",
    }


def test_proxy_endpoint_monkeypatch(client_factory) -> None:
    with client_factory() as client:
        client.app.state.services.gemini.proxy_call = _fake_proxy_call
        resp = client.post(
            "/proxy/gemini",
            json={"model": "gemini-2.5-flash", "contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


def test_gemini_compatible_route_monkeypatch(client_factory) -> None:
    with client_factory() as client:
        client.app.state.services.gemini.proxy_call = _fake_proxy_call
        resp = client.post(
            "/v1beta/models/gemini-2.5-flash:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
        )
        assert resp.status_code == 200
        assert resp.json()["via"] == "fake_proxy"


def test_proxy_default_endpoint_monkeypatch(client_factory) -> None:
    with client_factory() as client:
        client.app.state.services.gemini.proxy_call = _fake_proxy_call
        resp = client.post("/proxy/gemini/default")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


def test_all_admin_endpoints_happy_path(client_factory) -> None:
    with client_factory() as client:
        svc = client.app.state.services.gemini
        svc.get_models = _fake_models
        svc.get_models_summary = _fake_models_summary
        svc.refresh_models_cache = _fake_models_refresh
        svc.check_keys = _fake_keys_check
        svc.check_worker_connectivity = _fake_worker_connectivity
        svc.get_keys_status = lambda: {"last_checked_at": None, "total_keys": 1, "items": []}
        svc.get_rotation_state = lambda: {
            "mode": "cloudflare_worker",
            "key_pool_size": 2,
            "worker_pool_size": 1,
            "active_key_slot": 1,
            "active_worker_slot": 1,
            "keys": [{"slot": 1, "key_mask": "managed-by-worker", "is_active": True, "runtime": {}}],
            "workers": [{"slot": 1, "worker_url": "https://worker.example.test", "is_active": True, "runtime": {}}],
        }
        svc.get_recent_usage = lambda since_minutes=60: {
            "window_minutes": since_minutes,
            "requests_in_window": 0,
            "status_classes": {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "none": 0},
            "by_model": {},
            "by_key": {},
            "sample": [],
        }
        svc.get_limits_info = lambda: {
            "remaining_credit_per_key_supported": False,
            "remaining_credit_note": "x",
            "retry_policy": {"retry_on_429": True, "max_retries_per_key": 2, "cooloff_sec": 30},
            "recent_rate_limited_events": 0,
            "docs": {},
        }
        svc.get_incidents = lambda limit=100: {"total": 0, "returned": 0, "items": []}

        assert client.get("/health").status_code == 200
        assert client.get("/admin/system/config-effective").status_code == 200
        assert client.get("/admin/gemini/models").status_code == 200
        assert client.get("/admin/gemini/models/summary").status_code == 200
        assert client.post("/admin/gemini/models/refresh").status_code == 200
        assert client.get("/admin/keys/status").status_code == 200
        assert client.post("/admin/keys/check").status_code == 200
        assert client.get("/admin/keys/rotation-state").status_code == 200
        assert client.get("/admin/usage/recent?since_minutes=30").status_code == 200
        assert client.get("/admin/limits/info").status_code == 200
        assert client.get("/admin/worker/connectivity").status_code == 200
        assert client.get("/admin/incidents?limit=10").status_code == 200


def test_admin_auth_required(client_factory) -> None:
    with client_factory({"ADMIN_REQUIRE_AUTH": "true", "ADMIN_TOKEN": "super-secret", "ADMIN_HEADER_NAME": "x-admin-token"}) as client:
        denied = client.get("/admin/system/config-effective")
        assert denied.status_code == 401

        allowed = client.get("/admin/system/config-effective", headers={"x-admin-token": "super-secret"})
        assert allowed.status_code == 200


def test_admin_disabled(client_factory) -> None:
    with client_factory({"ADMIN_ENABLED": "false"}) as client:
        resp = client.get("/admin/system/config-effective")
        assert resp.status_code == 404


def test_config_effective_redacts_secrets(client_factory) -> None:
    with client_factory(
        {
            "ADMIN_REQUIRE_AUTH": "true",
            "ADMIN_TOKEN": "admin-token-value",
            "GEMINI_API_KEYS": "real-key-one,real-key-two",
            "CLOUDFLARE_AUTH_TOKEN": "secret-worker-token",
        }
    ) as client:
        resp = client.get("/admin/system/config-effective", headers={"x-admin-token": "admin-token-value"})
        assert resp.status_code == 200
        body = resp.json()
        cfg = body["effective_config"]

        assert cfg["gemini"]["api_keys_count"] == 2
        assert "real-key-one" not in str(cfg)
        assert "secret-worker-token" not in str(cfg)
