from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import HTTPException

from .config import Settings


class RateLimiter:
    def __init__(self, min_interval_sec: float) -> None:
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def wait(self) -> None:
        if self.min_interval_sec <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last_call
            if delta < self.min_interval_sec:
                await asyncio.sleep(self.min_interval_sec - delta)
            self._last_call = time.monotonic()


class RoundRobinManager:
    def __init__(self, values: List[str]) -> None:
        if not values:
            raise ValueError("RoundRobinManager requires at least one value")
        self.values = values
        self.index = 0

    @property
    def active(self) -> str:
        return self.values[self.index]

    def next(self) -> str:
        self.index = (self.index + 1) % len(self.values)
        return self.active


class GeminiProxyService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        timeout = httpx.Timeout(settings.app.request_timeout_sec)
        trust_env = bool(settings.proxy.trust_env_proxy)
        if trust_env:
            # httpx does not accept socks:// scheme from env; HTTP(S)_PROXY is enough for this setup.
            for key in ("ALL_PROXY", "all_proxy"):
                val = os.getenv(key, "")
                if val.lower().startswith("socks://"):
                    os.environ.pop(key, None)
        self.client = httpx.AsyncClient(timeout=timeout, trust_env=trust_env)
        self.limiter = RateLimiter(settings.proxy.min_interval_sec)

        self.worker_rr: Optional[RoundRobinManager] = None
        if settings.cloudflare.worker_base_urls:
            self.worker_rr = RoundRobinManager(settings.cloudflare.worker_base_urls)

        self.gemini_key_rr: Optional[RoundRobinManager] = None
        if settings.gemini.api_keys:
            self.gemini_key_rr = RoundRobinManager(settings.gemini.api_keys)

    async def aclose(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _is_429(resp: httpx.Response) -> bool:
        if resp.status_code == 429:
            return True
        txt = resp.text.upper()
        return "RESOURCE_EXHAUSTED" in txt

    @staticmethod
    def _join_url(base: str, suffix: str) -> str:
        return base.rstrip("/") + "/" + suffix.lstrip("/")

    def _build_worker_url(self, api_version: str, model: str, method: str) -> str:
        if not self.worker_rr:
            raise RuntimeError("No Cloudflare worker URL configured")
        base = self.worker_rr.active
        prefix = self.settings.cloudflare.worker_route_prefix.strip()
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        return self._join_url(base, f"{prefix}/{api_version}/models/{model}:{method}")

    def _build_direct_gemini_url(self, api_version: str, model: str, method: str) -> str:
        base = self.settings.gemini.base_url
        return self._join_url(base, f"{api_version}/models/{model}:{method}")

    def _build_headers(self, request_id: str | None) -> Dict[str, str]:
        headers: Dict[str, str] = {"content-type": "application/json"}
        if request_id and self.settings.cloudflare.pass_trace_headers:
            headers["x-request-id"] = request_id

        if self.settings.proxy.mode == "cloudflare_worker":
            if self.settings.cloudflare.auth_token:
                headers[self.settings.cloudflare.auth_header_name] = self.settings.cloudflare.auth_token
            if self.settings.cloudflare.access_client_id and self.settings.cloudflare.access_client_secret:
                headers["CF-Access-Client-Id"] = self.settings.cloudflare.access_client_id
                headers["CF-Access-Client-Secret"] = self.settings.cloudflare.access_client_secret
        else:
            if not self.gemini_key_rr:
                raise RuntimeError("No Gemini API key configured")
            headers["x-goog-api-key"] = self.gemini_key_rr.active

        return headers

    async def proxy_call(
        self,
        body: Dict[str, Any],
        *,
        path_model: str | None = None,
        path_api_version: str | None = None,
        path_method: str | None = None,
        request_id: str | None = None,
    ) -> httpx.Response:
        model = str(path_model or body.pop("model", self.settings.gemini.default_model))
        api_version = str(path_api_version or body.pop("api_version", self.settings.gemini.api_version))
        method = str(path_method or body.pop("method", "generateContent"))

        payload = dict(self.settings.gemini.default_request)
        payload.update(body)

        if "contents" not in payload:
            raise HTTPException(status_code=422, detail="`contents` is required in request payload")

        retry_on_429 = self.settings.proxy.retry_on_429
        rounds_limit = max(1, int(self.settings.proxy.max_retries_per_key))
        cooloff_sec = max(0.0, float(self.settings.proxy.cooloff_sec))

        rounds_done = 0
        tried_in_round = 0

        while True:
            await self.limiter.wait()
            headers = self._build_headers(request_id)

            if self.settings.proxy.mode == "cloudflare_worker":
                url = self._build_worker_url(api_version=api_version, model=model, method=method)
            else:
                url = self._build_direct_gemini_url(api_version=api_version, model=model, method=method)

            try:
                response = await self.client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Upstream connection error while calling worker/gemini: {exc}",
                ) from exc

            if not retry_on_429 or not self._is_429(response):
                return response

            pool_size = len(self.worker_rr.values) if self.settings.proxy.mode == "cloudflare_worker" else len(self.gemini_key_rr.values)  # type: ignore[union-attr]

            tried_in_round += 1
            if self.settings.proxy.mode == "cloudflare_worker":
                self.worker_rr.next()  # type: ignore[union-attr]
            else:
                self.gemini_key_rr.next()  # type: ignore[union-attr]

            if tried_in_round >= pool_size:
                rounds_done += 1
                tried_in_round = 0
                if rounds_done >= rounds_limit:
                    if cooloff_sec > 0:
                        await asyncio.sleep(cooloff_sec)
                    rounds_done = 0


class ServiceContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.gemini = GeminiProxyService(settings)

    async def close(self) -> None:
        await self.gemini.aclose()
