from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import HTTPException

from api.app.config import Settings
from api.app.services import GeminiProxyService


def _run(coro):
    return asyncio.run(coro)


def _settings(mode: str = "gemini_direct") -> Settings:
    s = Settings()
    s.proxy.mode = mode  # type: ignore[assignment]
    s.proxy.retry_on_429 = False
    s.proxy.min_interval_sec = 0
    s.gemini.api_keys = ["key-1-abcdef", "key-2-ghijkl"]
    s.cloudflare.worker_base_urls = ["https://worker-a.example", "https://worker-b.example"]
    s.admin.max_recent_requests = 500
    s.admin.max_incidents = 200
    return s


def test_get_recent_usage_aggregates() -> None:
    svc = GeminiProxyService(_settings("gemini_direct"))
    now = datetime.now(timezone.utc).isoformat()

    svc.request_history.append(
        {
            "ts": now,
            "mode": "gemini_direct",
            "api_version": "v1beta",
            "model": "gemini-2.5-flash",
            "method": "generateContent",
            "status_code": 200,
            "latency_ms": 10,
            "worker_slot": None,
            "worker_url": None,
            "key_slot": 1,
            "key_mask": "AIzaSy...1111",
            "usage_metadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 2,
                "thoughtsTokenCount": 1,
                "totalTokenCount": 6,
            },
            "error_message": "",
        }
    )
    svc.request_history.append(
        {
            "ts": now,
            "mode": "gemini_direct",
            "api_version": "v1beta",
            "model": "gemini-2.5-pro",
            "method": "generateContent",
            "status_code": 429,
            "latency_ms": 12,
            "worker_slot": None,
            "worker_url": None,
            "key_slot": 2,
            "key_mask": "AIzaSy...2222",
            "usage_metadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 0,
                "thoughtsTokenCount": 0,
                "totalTokenCount": 5,
            },
            "error_message": "rate",
        }
    )

    out = svc.get_recent_usage(since_minutes=60)
    assert out["requests_in_window"] == 2
    assert out["status_classes"]["2xx"] == 1
    assert out["status_classes"]["4xx"] == 1
    assert out["by_model"]["gemini-2.5-flash"]["total_tokens"] == 6
    assert out["by_key"]["AIzaSy...2222"]["prompt_tokens"] == 5

    _run(svc.aclose())


def test_get_rotation_state_includes_slots() -> None:
    svc = GeminiProxyService(_settings("cloudflare_worker"))
    state = svc.get_rotation_state()

    assert state["mode"] == "cloudflare_worker"
    assert state["worker_pool_size"] == 2
    assert state["key_pool_size"] == 2
    assert state["active_worker_slot"] == 1
    assert state["active_key_slot"] == 1

    _run(svc.aclose())


def test_get_keys_status_uses_runtime_and_health_snapshot() -> None:
    svc = GeminiProxyService(_settings("gemini_direct"))
    svc.key_runtime["key:1"] = {
        "id": "key:1",
        "label": "AIzaSy...1111",
        "success_count": 8,
        "failure_count": 2,
        "last_status": 200,
        "last_error": "",
        "last_429_at": None,
        "last_used_at": "x",
        "last_latency_ms": 15.2,
    }
    svc.key_health_snapshot = {
        "last_checked_at": "2026-01-01T00:00:00+00:00",
        "items": [
            {
                "slot": 1,
                "key_mask": "AIzaSy...1111",
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

    out = svc.get_keys_status()
    assert out["total_keys"] == 2
    assert out["items"][0]["valid"] is True
    assert out["items"][0]["runtime_success_count"] == 8

    _run(svc.aclose())


def test_get_limits_info_contains_expected_fields() -> None:
    svc = GeminiProxyService(_settings("gemini_direct"))
    svc.incidents.append({"kind": "rate_limited", "message": "x", "context": {}, "ts": "x"})

    out = svc.get_limits_info()
    assert out["remaining_credit_per_key_supported"] is False
    assert out["recent_rate_limited_events"] == 1
    assert "rate_limits" in out["docs"]

    _run(svc.aclose())


def test_get_models_filters_from_cache() -> None:
    svc = GeminiProxyService(_settings("gemini_direct"))
    svc._models_cache_payload = {
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "source_key_mask": "AIzaSy...1111",
        "total": 3,
        "models": [
            {
                "name": "models/gemini-2.5-flash",
                "display_name": "Gemini",
                "description": "",
                "input_token_limit": 1,
                "output_token_limit": 1,
                "supported_generation_methods": ["generateContent"],
                "is_preview": False,
            },
            {
                "name": "models/gemini-embedding-001",
                "display_name": "Emb",
                "description": "",
                "input_token_limit": 1,
                "output_token_limit": 1,
                "supported_generation_methods": ["embedContent"],
                "is_preview": False,
            },
            {
                "name": "models/gemini-3-pro-preview",
                "display_name": "Preview",
                "description": "",
                "input_token_limit": 1,
                "output_token_limit": 1,
                "supported_generation_methods": ["generateContent"],
                "is_preview": True,
            },
        ],
    }
    svc._models_cache_ts = 10**9

    async def _run_test() -> None:
        svc._models_cache_ts = asyncio.get_event_loop().time()
        gen_no_preview = await svc.get_models(capability="generate", include_preview=False)
        assert gen_no_preview["total"] == 1

        emb = await svc.get_models(capability="embed", include_preview=True)
        assert emb["total"] == 1

    _run(_run_test())
    _run(svc.aclose())


def test_check_worker_connectivity_classifies_access_redirect() -> None:
    svc = GeminiProxyService(_settings("cloudflare_worker"))

    async def _fake_post(*args, **kwargs):
        return httpx.Response(
            302,
            headers={"location": "https://example.cloudflareaccess.com/login"},
            request=httpx.Request("POST", "https://worker-a.example"),
        )

    svc.client.post = _fake_post  # type: ignore[method-assign]
    out = _run(svc.check_worker_connectivity())

    assert out["classification"] == "cloudflare_access_redirect"
    assert out["status_code"] == 302

    _run(svc.aclose())


def test_check_keys_with_mocked_http_calls() -> None:
    svc = GeminiProxyService(_settings("gemini_direct"))

    async def _fake_get(*args, **kwargs):
        return httpx.Response(200, json={"models": []}, request=httpx.Request("GET", "https://x"))

    async def _fake_post(*args, **kwargs):
        return httpx.Response(200, json={"candidates": []}, request=httpx.Request("POST", "https://x"))

    svc.client.get = _fake_get  # type: ignore[method-assign]
    svc.client.post = _fake_post  # type: ignore[method-assign]

    out = _run(svc.check_keys())
    assert out["ok"] is True
    assert len(out["items"]) == 2
    assert all(i["generate_test_ok"] for i in out["items"])

    _run(svc.aclose())


def test_get_models_in_cloudflare_mode_uses_worker_even_without_local_keys() -> None:
    svc = GeminiProxyService(_settings("cloudflare_worker"))
    svc.settings.gemini.api_keys = []

    async def _fake_get(url, *args, **kwargs):
        assert "worker-a.example" in str(url)
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-2.5-flash",
                        "displayName": "Gemini 2.5 Flash",
                        "description": "x",
                        "inputTokenLimit": 1,
                        "outputTokenLimit": 1,
                        "supportedGenerationMethods": ["generateContent", "countTokens"],
                    }
                ]
            },
            request=httpx.Request("GET", str(url)),
        )

    svc.client.get = _fake_get  # type: ignore[method-assign]
    out = _run(svc.get_models(capability="generate", include_preview=True, force_refresh=True))
    assert out["total"] == 1
    assert out["source_key_mask"] == "managed-by-worker"

    _run(svc.aclose())


def test_check_keys_in_cloudflare_mode_uses_worker_slots() -> None:
    svc = GeminiProxyService(_settings("cloudflare_worker"))
    svc.settings.gemini.api_keys = []

    async def _fake_get(url, *args, **kwargs):
        assert "/gemini/v1beta/models" in str(url)
        return httpx.Response(200, json={"models": []}, request=httpx.Request("GET", str(url)))

    async def _fake_post(url, *args, **kwargs):
        assert "/gemini/v1beta/models/" in str(url)
        return httpx.Response(200, json={"candidates": []}, request=httpx.Request("POST", str(url)))

    svc.client.get = _fake_get  # type: ignore[method-assign]
    svc.client.post = _fake_post  # type: ignore[method-assign]

    checked = _run(svc.check_keys())
    status = svc.get_keys_status()

    assert checked["ok"] is True
    assert len(checked["items"]) == 2
    assert checked["items"][0]["key_mask"] == "managed-by-worker"
    assert status["total_keys"] == 2
    assert status["items"][0]["worker_url"].startswith("https://worker-")

    _run(svc.aclose())


def test_proxy_call_records_connect_error_incident() -> None:
    svc = GeminiProxyService(_settings("gemini_direct"))

    async def _raise_connect(*args, **kwargs):
        raise httpx.ConnectError("boom")

    svc.client.post = _raise_connect  # type: ignore[method-assign]

    with pytest.raises(HTTPException) as exc:
        _run(
            svc.proxy_call(
                {
                    "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                },
                path_model="gemini-2.5-flash",
                path_api_version="v1beta",
                path_method="generateContent",
            )
        )

    assert exc.value.status_code == 502
    assert len(svc.incidents) >= 1

    _run(svc.aclose())


def test_proxy_call_sets_metadata_headers_in_direct_mode() -> None:
    svc = GeminiProxyService(_settings("gemini_direct"))

    async def _fake_post(*args, **kwargs):
        return httpx.Response(
            200,
            json={"candidates": []},
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent"),
        )

    svc.client.post = _fake_post  # type: ignore[method-assign]
    resp = _run(
        svc.proxy_call(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            path_model="gemini-2.5-flash",
            path_api_version="v1beta",
            path_method="generateContent",
        )
    )

    assert resp.headers.get("x-proxy-served-via") == "gemini_direct"
    assert resp.headers.get("x-proxy-key-slot") == "1"
    assert resp.headers.get("x-proxy-key-pool-size") == "2"
    assert resp.headers.get("x-proxy-attempts") == "1"
    assert resp.headers.get("x-proxy-key-rotated") == "false"

    _run(svc.aclose())


def test_proxy_call_passes_inline_image_data_to_upstream() -> None:
    svc = GeminiProxyService(_settings("gemini_direct"))

    async def _fake_post(*args, **kwargs):
        payload = kwargs.get("json") or {}
        parts = payload["contents"][0]["parts"]
        assert parts[0]["text"] == "Describe the image"
        assert parts[1]["inlineData"]["mimeType"] == "image/jpeg"
        assert parts[1]["inlineData"]["data"] == "ZmFrZS1pbWFnZS1ieXRlcw=="
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent"),
        )

    svc.client.post = _fake_post  # type: ignore[method-assign]
    resp = _run(
        svc.proxy_call(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": "Describe the image"},
                            {"inlineData": {"mimeType": "image/jpeg", "data": "ZmFrZS1pbWFnZS1ieXRlcw=="}},
                        ],
                    }
                ]
            },
            path_model="gemini-2.5-flash",
            path_api_version="v1beta",
            path_method="generateContent",
        )
    )

    assert resp.status_code == 200

    _run(svc.aclose())


def test_proxy_call_uses_worker_key_slot_header_for_runtime_tracking() -> None:
    svc = GeminiProxyService(_settings("cloudflare_worker"))

    async def _fake_post(*args, **kwargs):
        return httpx.Response(
            200,
            json={"candidates": []},
            headers={
                "content-type": "application/json",
                "x-proxy-served-via": "cloudflare_worker",
                "x-proxy-key-slot": "4",
                "x-proxy-key-pool-size": "10",
                "x-proxy-attempts": "3",
                "x-proxy-key-rotated": "true",
            },
            request=httpx.Request("POST", "https://worker-a.example/gemini/v1beta/models/x:generateContent"),
        )

    svc.client.post = _fake_post  # type: ignore[method-assign]
    resp = _run(
        svc.proxy_call(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            path_model="gemini-2.5-flash",
            path_api_version="v1beta",
            path_method="generateContent",
        )
    )

    assert resp.headers.get("x-proxy-key-slot") == "4"
    assert resp.headers.get("x-proxy-key-pool-size") == "10"
    assert resp.headers.get("x-proxy-attempts") == "3"
    assert resp.headers.get("x-proxy-key-rotated") == "true"
    assert svc.request_history[-1]["key_slot"] == 4
    assert svc.request_history[-1]["key_mask"] == "worker-key-slot-4"

    _run(svc.aclose())


def test_proxy_call_retries_on_5xx_and_succeeds() -> None:
    svc = GeminiProxyService(_settings("gemini_direct"))
    svc.settings.proxy.retry_on_5xx = True
    svc.settings.proxy.max_retries_on_5xx = 3
    svc.settings.proxy.retry_backoff_sec = 0

    statuses = [500, 500, 200]

    async def _fake_post(*args, **kwargs):
        status = statuses.pop(0)
        return httpx.Response(
            status,
            json={"candidates": []} if status == 200 else {"error": {"status": "INTERNAL"}},
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent"),
        )

    svc.client.post = _fake_post  # type: ignore[method-assign]
    resp = _run(
        svc.proxy_call(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            path_model="gemma-4-26b-a4b-it",
            path_api_version="v1beta",
            path_method="generateContent",
        )
    )

    assert resp.status_code == 200
    assert resp.headers.get("x-proxy-attempts") == "3"
    assert [r["status_code"] for r in svc.request_history][-3:] == [500, 500, 200]
    assert any(i["kind"] == "retry_on_5xx" for i in svc.incidents)

    _run(svc.aclose())


def test_proxy_call_stops_after_max_5xx_retries() -> None:
    svc = GeminiProxyService(_settings("gemini_direct"))
    svc.settings.proxy.retry_on_5xx = True
    svc.settings.proxy.max_retries_on_5xx = 2
    svc.settings.proxy.retry_backoff_sec = 0

    async def _fake_post(*args, **kwargs):
        return httpx.Response(
            500,
            json={"error": {"status": "INTERNAL"}},
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent"),
        )

    svc.client.post = _fake_post  # type: ignore[method-assign]
    resp = _run(
        svc.proxy_call(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            path_model="gemma-4-26b-a4b-it",
            path_api_version="v1beta",
            path_method="generateContent",
        )
    )

    assert resp.status_code == 500
    assert resp.headers.get("x-proxy-attempts") == "3"
    assert [r["status_code"] for r in svc.request_history][-3:] == [500, 500, 500]
    assert sum(1 for i in svc.incidents if i["kind"] == "retry_on_5xx") >= 2

    _run(svc.aclose())
