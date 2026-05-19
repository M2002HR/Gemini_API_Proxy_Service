from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

import httpx
from fastapi import HTTPException

from .config import Settings

logger = logging.getLogger(__name__)


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
        transport: httpx.AsyncHTTPTransport | None = None
        if bool(getattr(settings.proxy, "force_ipv4", False)):
            # Prefer IPv4 when IPv6 routing is flaky to avoid connect timeouts.
            transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        self.client = httpx.AsyncClient(timeout=timeout, trust_env=trust_env, transport=transport)
        self.limiter = RateLimiter(settings.proxy.min_interval_sec)

        self.worker_rr: Optional[RoundRobinManager] = None
        if settings.cloudflare.worker_base_urls:
            self.worker_rr = RoundRobinManager(settings.cloudflare.worker_base_urls)

        self.gemini_key_rr: Optional[RoundRobinManager] = None
        if settings.gemini.api_keys:
            self.gemini_key_rr = RoundRobinManager(settings.gemini.api_keys)

        self.started_at = self._now_iso()
        self.request_history: Deque[Dict[str, Any]] = deque(maxlen=settings.admin.max_recent_requests)
        self.incidents: Deque[Dict[str, Any]] = deque(maxlen=settings.admin.max_incidents)
        self.key_runtime: Dict[str, Dict[str, Any]] = {}
        self.worker_runtime: Dict[str, Dict[str, Any]] = {}
        self.key_health_snapshot: Dict[str, Any] = {"last_checked_at": None, "items": []}

        self._models_cache_payload: Optional[Dict[str, Any]] = None
        self._models_cache_ts: float = 0.0

    async def aclose(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _is_429(resp: httpx.Response) -> bool:
        if resp.status_code == 429:
            return True
        txt = resp.text.upper()
        return "RESOURCE_EXHAUSTED" in txt

    @staticmethod
    def _join_url(base: str, suffix: str) -> str:
        return base.rstrip("/") + "/" + suffix.lstrip("/")

    @staticmethod
    def _mask_key(key: str) -> str:
        if len(key) <= 10:
            return key[:3] + "***"
        return key[:6] + "..." + key[-4:]

    @staticmethod
    def _is_preview_model(model_name: str) -> bool:
        return "preview" in model_name.lower()

    def _runtime_entry(self, pool: Dict[str, Dict[str, Any]], rid: str, label: str) -> Dict[str, Any]:
        entry = pool.get(rid)
        if entry is None:
            entry = {
                "id": rid,
                "label": label,
                "success_count": 0,
                "failure_count": 0,
                "last_status": None,
                "last_error": "",
                "last_429_at": None,
                "last_used_at": None,
                "last_latency_ms": None,
            }
            pool[rid] = entry
        return entry

    def _record_incident(self, kind: str, message: str, context: Optional[Dict[str, Any]] = None) -> None:
        payload = {
            "ts": self._now_iso(),
            "kind": kind,
            "message": message,
            "context": context or {},
        }
        self.incidents.append(payload)
        logger.warning(
            "Gemini incident",
            extra={"details": payload},
        )

    def _record_request(
        self,
        *,
        request_id: str | None,
        mode: str,
        api_version: str,
        model: str,
        method: str,
        status_code: Optional[int],
        latency_ms: float,
        worker_slot: Optional[int],
        worker_url: Optional[str],
        key_slot: Optional[int],
        key_mask: Optional[str],
        usage_metadata: Optional[Dict[str, Any]],
        error_message: str,
    ) -> None:
        self.request_history.append(
            {
                "ts": self._now_iso(),
                "request_id": request_id,
                "mode": mode,
                "api_version": api_version,
                "model": model,
                "method": method,
                "status_code": status_code,
                "latency_ms": round(latency_ms, 2),
                "worker_slot": worker_slot,
                "worker_url": worker_url,
                "key_slot": key_slot,
                "key_mask": key_mask,
                "usage_metadata": usage_metadata or {},
                "error_message": error_message,
            }
        )

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

    @staticmethod
    def _usage_counts(usage_metadata: Dict[str, Any]) -> Dict[str, int]:
        return {
            "prompt_tokens": int(usage_metadata.get("promptTokenCount") or 0),
            "candidate_tokens": int(usage_metadata.get("candidatesTokenCount") or 0),
            "thoughts_tokens": int(usage_metadata.get("thoughtsTokenCount") or 0),
            "total_tokens": int(usage_metadata.get("totalTokenCount") or 0),
        }

    @staticmethod
    def _parse_positive_int(value: Any) -> Optional[int]:
        try:
            parsed = int(str(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _response_excerpt(response: httpx.Response, *, limit: int = 400) -> str:
        raw = " ".join((response.text or "").split())
        if len(raw) <= limit:
            return raw
        return raw[: limit - 3] + "..."

    def _set_proxy_metadata_headers(
        self,
        response: httpx.Response,
        *,
        worker_slot: Optional[int],
        worker_url: Optional[str],
        key_slot: Optional[int],
        key_pool_size: Optional[int],
        attempts: int,
        key_rotated: bool,
    ) -> None:
        response.headers["x-proxy-served-via"] = self.settings.proxy.mode
        response.headers["x-proxy-attempts"] = str(max(1, attempts))
        response.headers["x-proxy-key-rotated"] = "true" if key_rotated else "false"

        if worker_slot is not None:
            response.headers["x-proxy-worker-slot"] = str(worker_slot)
        if worker_url:
            response.headers["x-proxy-worker-url"] = worker_url
        if key_slot is not None:
            response.headers["x-proxy-key-slot"] = str(key_slot)
        if key_pool_size is not None:
            response.headers["x-proxy-key-pool-size"] = str(key_pool_size)

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

        if method in {"generateContent", "streamGenerateContent"} and "contents" not in payload:
            raise HTTPException(status_code=422, detail="`contents` is required in request payload")

        retry_on_429 = self.settings.proxy.retry_on_429
        retry_on_5xx = bool(self.settings.proxy.retry_on_5xx)
        rounds_limit = max(1, int(self.settings.proxy.max_retries_per_key))
        max_retries_on_5xx = max(0, int(self.settings.proxy.max_retries_on_5xx))
        retry_backoff_sec = max(0.0, float(self.settings.proxy.retry_backoff_sec))
        cooloff_sec = max(0.0, float(self.settings.proxy.cooloff_sec))

        rounds_done = 0
        tried_in_round = 0
        attempts = 0
        retries_5xx_done = 0
        retries_connect_done = 0
        request_started_at = time.monotonic()
        logger.info(
            "Gemini proxy call started",
            extra={
                "details": {
                    "request_id": request_id,
                    "mode": self.settings.proxy.mode,
                    "api_version": api_version,
                    "model": model,
                    "method": method,
                    "retry_on_429": retry_on_429,
                    "retry_on_5xx": retry_on_5xx,
                    "max_retries_per_key": rounds_limit,
                    "max_retries_on_5xx": max_retries_on_5xx,
                }
            },
        )

        while True:
            attempts += 1
            await self.limiter.wait()

            active_worker_slot: Optional[int] = None
            active_worker_url: Optional[str] = None
            active_key_slot: Optional[int] = None
            active_key_mask: Optional[str] = None

            if self.settings.proxy.mode == "cloudflare_worker":
                if not self.worker_rr:
                    raise RuntimeError("No Cloudflare worker URL configured")
                active_worker_slot = self.worker_rr.index + 1
                active_worker_url = self.worker_rr.active
                url = self._build_worker_url(api_version=api_version, model=model, method=method)
            else:
                if not self.gemini_key_rr:
                    raise RuntimeError("No Gemini API key configured")
                active_key_slot = self.gemini_key_rr.index + 1
                active_key_mask = self._mask_key(self.gemini_key_rr.active)
                url = self._build_direct_gemini_url(api_version=api_version, model=model, method=method)

            headers = self._build_headers(request_id)
            started = time.monotonic()
            logger.info(
                "Gemini proxy upstream attempt",
                extra={
                    "details": {
                        "request_id": request_id,
                        "attempt": attempts,
                        "mode": self.settings.proxy.mode,
                        "model": model,
                        "method": method,
                        "api_version": api_version,
                        "worker_slot": active_worker_slot,
                        "worker_url": active_worker_url,
                        "key_slot": active_key_slot,
                    }
                },
            )

            try:
                response = await self.client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                latency_ms = (time.monotonic() - started) * 1000.0
                self._record_request(
                    request_id=request_id,
                    mode=self.settings.proxy.mode,
                    api_version=api_version,
                    model=model,
                    method=method,
                    status_code=None,
                    latency_ms=latency_ms,
                    worker_slot=active_worker_slot,
                    worker_url=active_worker_url,
                    key_slot=active_key_slot,
                    key_mask=active_key_mask,
                    usage_metadata=None,
                    error_message=str(exc),
                )
                self._record_incident(
                    kind="upstream_connect_error",
                    message=str(exc),
                    context={
                        "request_id": request_id,
                        "mode": self.settings.proxy.mode,
                        "model": model,
                        "method": method,
                        "attempt": attempts,
                        "worker_slot": active_worker_slot,
                        "key_slot": active_key_slot,
                    },
                )
                logger.exception(
                    "Gemini proxy upstream connection error",
                    extra={
                        "details": {
                            "request_id": request_id,
                            "attempt": attempts,
                            "mode": self.settings.proxy.mode,
                            "model": model,
                            "method": method,
                            "worker_slot": active_worker_slot,
                            "key_slot": active_key_slot,
                            "latency_ms": round(latency_ms, 2),
                            "url": url,
                        }
                    },
                )
                should_retry_connect = retries_connect_done < max_retries_on_5xx
                if should_retry_connect:
                    retries_connect_done += 1
                    self._record_incident(
                        kind="retry_on_connect_error",
                        message=(
                            f"retry_on_connect_error #{retries_connect_done}/{max_retries_on_5xx} "
                            f"model={model} method={method}"
                        ),
                        context={
                            "request_id": request_id,
                            "mode": self.settings.proxy.mode,
                            "worker_slot": active_worker_slot,
                            "key_slot": active_key_slot,
                            "attempt": attempts,
                        },
                    )
                    logger.warning(
                        "Gemini proxy retrying after upstream connect error",
                        extra={
                            "details": {
                                "request_id": request_id,
                                "attempt": attempts,
                                "retry_number": retries_connect_done,
                                "max_retries_on_5xx": max_retries_on_5xx,
                                "mode": self.settings.proxy.mode,
                                "model": model,
                                "method": method,
                                "worker_slot": active_worker_slot,
                                "key_slot": active_key_slot,
                            }
                        },
                    )

                    pool_size = (
                        len(self.worker_rr.values)
                        if self.settings.proxy.mode == "cloudflare_worker"
                        else len(self.gemini_key_rr.values)  # type: ignore[union-attr]
                    )
                    tried_in_round += 1
                    if self.settings.proxy.mode == "cloudflare_worker":
                        self.worker_rr.next()  # type: ignore[union-attr]
                    else:
                        self.gemini_key_rr.next()  # type: ignore[union-attr]

                    if tried_in_round >= pool_size:
                        rounds_done += 1
                        tried_in_round = 0
                        if rounds_done >= rounds_limit:
                            logger.warning(
                                "Gemini proxy exhausted one full retry round",
                                extra={
                                    "details": {
                                        "request_id": request_id,
                                        "rounds_done": rounds_done,
                                        "rounds_limit": rounds_limit,
                                        "pool_size": pool_size,
                                        "cooloff_sec": cooloff_sec,
                                    }
                                },
                            )
                            if cooloff_sec > 0:
                                await asyncio.sleep(cooloff_sec)
                            rounds_done = 0

                    if retry_backoff_sec > 0:
                        await asyncio.sleep(retry_backoff_sec)
                    continue

                raise HTTPException(
                    status_code=502,
                    detail=f"Upstream connection error while calling worker/gemini: {exc}",
                ) from exc

            latency_ms = (time.monotonic() - started) * 1000.0
            usage_metadata: Dict[str, Any] = {}
            if "application/json" in (response.headers.get("content-type", "").lower()):
                try:
                    usage_metadata = response.json().get("usageMetadata", {}) or {}
                except Exception:
                    usage_metadata = {}

            key_pool_size: Optional[int] = None
            key_rotated = attempts > 1
            if self.settings.proxy.mode == "cloudflare_worker":
                worker_key_slot = self._parse_positive_int(response.headers.get("x-proxy-key-slot"))
                if worker_key_slot is not None:
                    active_key_slot = worker_key_slot
                    active_key_mask = f"worker-key-slot-{worker_key_slot}"
                key_pool_size = self._parse_positive_int(response.headers.get("x-proxy-key-pool-size"))
                if key_pool_size is None and self.settings.gemini.api_keys:
                    key_pool_size = len(self.settings.gemini.api_keys)
                key_rotated = str(response.headers.get("x-proxy-key-rotated", "")).strip().lower() == "true" or key_rotated
            else:
                key_pool_size = len(self.gemini_key_rr.values) if self.gemini_key_rr else None

            self._set_proxy_metadata_headers(
                response,
                worker_slot=active_worker_slot,
                worker_url=active_worker_url,
                key_slot=active_key_slot,
                key_pool_size=key_pool_size,
                attempts=self._parse_positive_int(response.headers.get("x-proxy-attempts")) or attempts,
                key_rotated=key_rotated,
            )

            self._record_request(
                request_id=request_id,
                mode=self.settings.proxy.mode,
                api_version=api_version,
                model=model,
                method=method,
                status_code=response.status_code,
                latency_ms=latency_ms,
                worker_slot=active_worker_slot,
                worker_url=active_worker_url,
                key_slot=active_key_slot,
                key_mask=active_key_mask,
                usage_metadata=usage_metadata,
                error_message="",
            )
            logger.info(
                "Gemini proxy upstream response",
                extra={
                    "details": {
                        "request_id": request_id,
                        "attempt": attempts,
                        "mode": self.settings.proxy.mode,
                        "model": model,
                        "method": method,
                        "status_code": response.status_code,
                        "latency_ms": round(latency_ms, 2),
                        "worker_slot": active_worker_slot,
                        "key_slot": active_key_slot,
                        "usage_tokens": self._usage_counts(usage_metadata),
                    }
                },
            )

            if self.settings.proxy.mode == "cloudflare_worker":
                rid = f"worker:{active_worker_slot}"
                entry = self._runtime_entry(self.worker_runtime, rid, active_worker_url or "")
            else:
                rid = f"key:{active_key_slot}"
                entry = self._runtime_entry(self.key_runtime, rid, active_key_mask or "")

            entry["last_used_at"] = self._now_iso()
            entry["last_latency_ms"] = round(latency_ms, 2)
            entry["last_status"] = response.status_code

            if response.status_code >= 400:
                entry["failure_count"] += 1
                message = f"upstream status={response.status_code} model={model} method={method}"
                entry["last_error"] = message
                self._record_incident(
                    kind="upstream_http_error",
                    message=message,
                    context={
                        "request_id": request_id,
                        "mode": self.settings.proxy.mode,
                        "worker_slot": active_worker_slot,
                        "key_slot": active_key_slot,
                        "attempt": attempts,
                        "status_code": response.status_code,
                    },
                )
                logger.warning(
                    "Gemini proxy upstream returned error",
                    extra={
                        "details": {
                            "request_id": request_id,
                            "attempt": attempts,
                            "status_code": response.status_code,
                            "mode": self.settings.proxy.mode,
                            "model": model,
                            "method": method,
                            "worker_slot": active_worker_slot,
                            "key_slot": active_key_slot,
                            "body_excerpt": self._response_excerpt(response),
                        }
                    },
                )
            else:
                entry["success_count"] += 1
                entry["last_error"] = ""

            should_retry_429 = retry_on_429 and self._is_429(response)
            should_retry_5xx = retry_on_5xx and response.status_code >= 500 and retries_5xx_done < max_retries_on_5xx

            if not should_retry_429 and not should_retry_5xx:
                logger.info(
                    "Gemini proxy call finished",
                    extra={
                        "details": {
                            "request_id": request_id,
                            "mode": self.settings.proxy.mode,
                            "model": model,
                            "method": method,
                            "status_code": response.status_code,
                            "attempts": attempts,
                            "total_latency_ms": round((time.monotonic() - request_started_at) * 1000.0, 2),
                            "worker_slot": active_worker_slot,
                            "key_slot": active_key_slot,
                        }
                    },
                )
                return response

            if should_retry_429:
                entry["last_429_at"] = self._now_iso()
                message = f"429/resource_exhausted at {model}:{method}"
                self._record_incident(
                    kind="rate_limited",
                    message=message,
                    context={
                        "request_id": request_id,
                        "mode": self.settings.proxy.mode,
                        "worker_slot": active_worker_slot,
                        "key_slot": active_key_slot,
                        "attempt": attempts,
                    },
                )
                logger.warning(
                    "Gemini proxy retrying after 429/resource exhausted",
                    extra={
                        "details": {
                            "request_id": request_id,
                            "attempt": attempts,
                            "mode": self.settings.proxy.mode,
                            "model": model,
                            "method": method,
                            "worker_slot": active_worker_slot,
                            "key_slot": active_key_slot,
                        }
                    },
                )
            else:
                retries_5xx_done += 1
                message = (
                    f"retry_on_5xx #{retries_5xx_done}/{max_retries_on_5xx} "
                    f"status={response.status_code} model={model} method={method}"
                )
                self._record_incident(
                    kind="retry_on_5xx",
                    message=message,
                    context={
                        "request_id": request_id,
                        "mode": self.settings.proxy.mode,
                        "worker_slot": active_worker_slot,
                        "key_slot": active_key_slot,
                        "attempt": attempts,
                        "status_code": response.status_code,
                        "retry_number": retries_5xx_done,
                    },
                )
                logger.warning(
                    "Gemini proxy retrying after upstream 5xx",
                    extra={
                        "details": {
                            "request_id": request_id,
                            "attempt": attempts,
                            "retry_number": retries_5xx_done,
                            "max_retries_on_5xx": max_retries_on_5xx,
                            "status_code": response.status_code,
                            "mode": self.settings.proxy.mode,
                            "model": model,
                            "method": method,
                            "worker_slot": active_worker_slot,
                            "key_slot": active_key_slot,
                        }
                    },
                )

            pool_size = (
                len(self.worker_rr.values)
                if self.settings.proxy.mode == "cloudflare_worker"
                else len(self.gemini_key_rr.values)  # type: ignore[union-attr]
            )

            tried_in_round += 1
            if self.settings.proxy.mode == "cloudflare_worker":
                self.worker_rr.next()  # type: ignore[union-attr]
            else:
                self.gemini_key_rr.next()  # type: ignore[union-attr]

            if tried_in_round >= pool_size:
                rounds_done += 1
                tried_in_round = 0
                if rounds_done >= rounds_limit:
                    logger.warning(
                        "Gemini proxy exhausted one full retry round",
                        extra={
                            "details": {
                                "request_id": request_id,
                                "rounds_done": rounds_done,
                                "rounds_limit": rounds_limit,
                                "pool_size": pool_size,
                                "cooloff_sec": cooloff_sec,
                            }
                        },
                    )
                    if cooloff_sec > 0:
                        await asyncio.sleep(cooloff_sec)
                    rounds_done = 0

            if should_retry_5xx and retry_backoff_sec > 0:
                logger.info(
                    "Gemini proxy retry backoff sleep",
                    extra={
                        "details": {
                            "request_id": request_id,
                            "retry_backoff_sec": retry_backoff_sec,
                            "attempt": attempts,
                        }
                    },
                )
                await asyncio.sleep(retry_backoff_sec)

    def _redact_model(self, model: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": model.get("name"),
            "display_name": model.get("displayName"),
            "description": model.get("description"),
            "input_token_limit": model.get("inputTokenLimit"),
            "output_token_limit": model.get("outputTokenLimit"),
            "supported_generation_methods": model.get("supportedGenerationMethods", []),
            "is_preview": self._is_preview_model(str(model.get("name", ""))),
        }

    def _build_worker_models_list_url(self, api_version: str) -> str:
        if not self.worker_rr:
            raise RuntimeError("No Cloudflare worker URL configured")
        base = self.worker_rr.active
        prefix = self.settings.cloudflare.worker_route_prefix.strip()
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        return self._join_url(base, f"{prefix}/{api_version}/models?pageSize=1000")

    async def _fetch_models_from_gemini_direct(self) -> Dict[str, Any]:
        if not self.settings.gemini.api_keys:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No GEMINI_API_KEYS configured in FastAPI environment. "
                    "Set GEMINI_API_KEYS to enable /admin/gemini/models endpoints."
                ),
            )

        api_key = self.settings.gemini.api_keys[0]
        headers = {"x-goog-api-key": api_key}
        url = self._join_url(self.settings.gemini.base_url, f"{self.settings.gemini.api_version}/models?pageSize=1000")
        resp = await self.client.get(url, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)

        raw_models = resp.json().get("models", [])
        models = [self._redact_model(m) for m in raw_models]
        payload = {
            "fetched_at": self._now_iso(),
            "source_key_mask": self._mask_key(api_key),
            "models": models,
            "total": len(models),
        }
        self._models_cache_payload = payload
        self._models_cache_ts = time.monotonic()
        return payload

    async def _fetch_models_from_worker(self) -> Dict[str, Any]:
        if not self.worker_rr:
            raise HTTPException(status_code=503, detail="No worker URL configured for cloudflare_worker mode")

        url = self._build_worker_models_list_url(self.settings.gemini.api_version)
        headers = self._build_headers(request_id=None)
        try:
            resp = await self.client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Worker list models connection error: {exc}") from exc

        if resp.status_code != 200:
            detail = resp.text
            if resp.status_code == 405 and "Method Not Allowed" in resp.text:
                detail = (
                    "Worker does not support GET /<prefix>/<apiVersion>/models yet. "
                    "Deploy the latest worker code that includes models-list proxy support."
                )
                raise HTTPException(status_code=502, detail=detail)
            raise HTTPException(status_code=resp.status_code, detail=detail)

        raw_models = resp.json().get("models", [])
        models = [self._redact_model(m) for m in raw_models]
        payload = {
            "fetched_at": self._now_iso(),
            "source_key_mask": "managed-by-worker",
            "models": models,
            "total": len(models),
        }
        self._models_cache_payload = payload
        self._models_cache_ts = time.monotonic()
        return payload

    async def _fetch_models_from_gemini(self) -> Dict[str, Any]:
        if self.settings.proxy.mode == "cloudflare_worker":
            return await self._fetch_models_from_worker()
        return await self._fetch_models_from_gemini_direct()

    async def get_models(
        self,
        *,
        capability: str = "all",
        include_preview: bool = True,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        ttl = max(0.0, float(self.settings.admin.models_cache_ttl_sec))
        cached_ok = self._models_cache_payload is not None and (time.monotonic() - self._models_cache_ts) <= ttl

        if force_refresh or not cached_ok:
            payload = await self._fetch_models_from_gemini()
            from_cache = False
        else:
            payload = self._models_cache_payload or {"models": [], "total": 0, "fetched_at": self._now_iso()}
            from_cache = True

        models = list(payload.get("models", []))

        capability_method = {
            "all": "",
            "generate": "generateContent",
            "embed": "embedContent",
            "live": "bidiGenerateContent",
        }[capability]

        if capability_method:
            models = [m for m in models if capability_method in (m.get("supported_generation_methods") or [])]

        if not include_preview:
            models = [m for m in models if not bool(m.get("is_preview"))]

        return {
            "capability": capability,
            "include_preview": include_preview,
            "from_cache": from_cache,
            "fetched_at": payload.get("fetched_at"),
            "source_key_mask": payload.get("source_key_mask"),
            "total": len(models),
            "models": models,
        }

    async def refresh_models_cache(self) -> Dict[str, Any]:
        payload = await self._fetch_models_from_gemini()
        return {
            "ok": True,
            "fetched_at": payload.get("fetched_at"),
            "total": payload.get("total", 0),
            "source_key_mask": payload.get("source_key_mask"),
        }

    async def get_models_summary(self) -> Dict[str, Any]:
        payload = await self.get_models(capability="all", include_preview=True, force_refresh=False)
        models = payload.get("models", [])

        generate_count = sum(
            1 for m in models if "generateContent" in (m.get("supported_generation_methods") or [])
        )
        embed_count = sum(1 for m in models if "embedContent" in (m.get("supported_generation_methods") or []))
        live_count = sum(
            1 for m in models if "bidiGenerateContent" in (m.get("supported_generation_methods") or [])
        )
        preview_count = sum(1 for m in models if bool(m.get("is_preview")))

        return {
            "from_cache": payload.get("from_cache", False),
            "fetched_at": payload.get("fetched_at"),
            "total_models": len(models),
            "generate_models": generate_count,
            "embed_models": embed_count,
            "live_models": live_count,
            "preview_models": preview_count,
            "stable_models": len(models) - preview_count,
        }

    async def _check_keys_direct(self) -> Dict[str, Any]:
        keys = list(self.settings.gemini.api_keys)
        if not keys:
            self.key_health_snapshot = {
                "last_checked_at": self._now_iso(),
                "items": [],
                "note": "No GEMINI_API_KEYS configured in FastAPI for direct key checks.",
            }
            return {
                "ok": False,
                "last_checked_at": self.key_health_snapshot["last_checked_at"],
                "items": [],
                "note": self.key_health_snapshot["note"],
            }

        items: List[Dict[str, Any]] = []
        model_name = self.settings.gemini.default_model
        list_url = self._join_url(self.settings.gemini.base_url, f"{self.settings.gemini.api_version}/models?pageSize=1")
        gen_url = self._join_url(
            self.settings.gemini.base_url,
            f"{self.settings.gemini.api_version}/models/{model_name}:generateContent",
        )

        for idx, key in enumerate(keys, start=1):
            key_mask = self._mask_key(key)
            headers = {"x-goog-api-key": key, "content-type": "application/json"}

            list_started = time.monotonic()
            list_resp = await self.client.get(list_url, headers=headers)
            list_latency_ms = (time.monotonic() - list_started) * 1000.0

            list_ok = list_resp.status_code == 200
            list_error = ""
            if not list_ok:
                list_error = list_resp.text[:300]

            gen_ok = False
            gen_status = None
            gen_latency_ms = None
            gen_error = ""
            if list_ok:
                payload = {
                    "contents": [{"role": "user", "parts": [{"text": "Reply with: ok"}]}],
                    "generationConfig": {"maxOutputTokens": 8, "temperature": 0},
                }
                gen_started = time.monotonic()
                gen_resp = await self.client.post(gen_url, headers=headers, json=payload)
                gen_latency_ms = (time.monotonic() - gen_started) * 1000.0
                gen_status = gen_resp.status_code
                gen_ok = gen_resp.status_code == 200
                if not gen_ok:
                    gen_error = gen_resp.text[:300]

            items.append(
                {
                    "slot": idx,
                    "key_mask": key_mask,
                    "models_access_ok": list_ok,
                    "models_status": list_resp.status_code,
                    "models_latency_ms": round(list_latency_ms, 2),
                    "models_error": list_error,
                    "generate_test_ok": gen_ok,
                    "generate_status": gen_status,
                    "generate_latency_ms": round(gen_latency_ms, 2) if gen_latency_ms is not None else None,
                    "generate_error": gen_error,
                }
            )

        self.key_health_snapshot = {
            "last_checked_at": self._now_iso(),
            "items": items,
        }

        return {
            "ok": True,
            "last_checked_at": self.key_health_snapshot["last_checked_at"],
            "items": items,
        }

    async def _check_worker_slots(self) -> Dict[str, Any]:
        if not self.settings.cloudflare.worker_base_urls:
            self.key_health_snapshot = {
                "last_checked_at": self._now_iso(),
                "items": [],
                "note": "No worker URLs configured.",
            }
            return {
                "ok": False,
                "last_checked_at": self.key_health_snapshot["last_checked_at"],
                "items": [],
                "note": self.key_health_snapshot["note"],
            }

        items: List[Dict[str, Any]] = []
        model_name = self.settings.gemini.default_model
        list_api_version = self.settings.gemini.api_version
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "Reply with: ok"}]}],
            "generationConfig": {"maxOutputTokens": 8, "temperature": 0},
        }

        original_idx = self.worker_rr.index if self.worker_rr else 0

        overall_ok = True
        for idx, worker_url in enumerate(self.settings.cloudflare.worker_base_urls, start=1):
            if self.worker_rr:
                self.worker_rr.index = idx - 1

            list_url = self._build_worker_models_list_url(list_api_version)
            headers = self._build_headers(request_id=None)

            list_started = time.monotonic()
            list_resp = await self.client.get(list_url, headers=headers)
            list_latency_ms = (time.monotonic() - list_started) * 1000.0
            list_ok = list_resp.status_code == 200
            list_error = "" if list_ok else list_resp.text[:300]
            if not list_ok:
                overall_ok = False

            gen_status = None
            gen_ok = False
            gen_error = ""
            gen_latency_ms = None

            gen_url = self._join_url(worker_url, f"{self.settings.cloudflare.worker_route_prefix}/{list_api_version}/models/{model_name}:generateContent")
            gen_started = time.monotonic()
            gen_resp = await self.client.post(gen_url, headers=headers, json=payload)
            gen_latency_ms = (time.monotonic() - gen_started) * 1000.0
            gen_status = gen_resp.status_code
            gen_ok = gen_resp.status_code == 200
            if not gen_ok:
                gen_error = gen_resp.text[:300]
                overall_ok = False

            items.append(
                {
                    "slot": idx,
                    "key_mask": "managed-by-worker",
                    "worker_url": worker_url,
                    "models_access_ok": list_ok,
                    "models_status": list_resp.status_code,
                    "models_latency_ms": round(list_latency_ms, 2),
                    "models_error": list_error,
                    "generate_test_ok": gen_ok,
                    "generate_status": gen_status,
                    "generate_latency_ms": round(gen_latency_ms, 2) if gen_latency_ms is not None else None,
                    "generate_error": gen_error,
                }
            )

        if self.worker_rr:
            self.worker_rr.index = original_idx

        self.key_health_snapshot = {
            "last_checked_at": self._now_iso(),
            "items": items,
            "note": "cloudflare_worker mode hides actual Gemini API keys behind Worker.",
        }

        return {
            "ok": overall_ok,
            "last_checked_at": self.key_health_snapshot["last_checked_at"],
            "items": items,
            "note": self.key_health_snapshot["note"],
        }

    async def check_keys(self) -> Dict[str, Any]:
        if self.settings.proxy.mode == "cloudflare_worker":
            return await self._check_worker_slots()
        return await self._check_keys_direct()

    def _success_rate_for_key(self, key_slot: int, lookback_sec: int = 3600) -> Optional[float]:
        cutoff = datetime.now(timezone.utc).timestamp() - lookback_sec
        total = 0
        success = 0
        for rec in self.request_history:
            if rec.get("key_slot") != key_slot:
                continue
            try:
                ts = datetime.fromisoformat(str(rec.get("ts"))).timestamp()
            except Exception:
                continue
            if ts < cutoff:
                continue
            total += 1
            status_code = rec.get("status_code")
            if isinstance(status_code, int) and status_code < 400:
                success += 1
        if total == 0:
            return None
        return round(success / total, 4)

    def get_keys_status(self) -> Dict[str, Any]:
        checks_by_slot: Dict[int, Dict[str, Any]] = {}
        for item in self.key_health_snapshot.get("items", []):
            checks_by_slot[int(item.get("slot", 0))] = item

        if self.settings.proxy.mode == "cloudflare_worker":
            items: List[Dict[str, Any]] = []
            for idx, worker_url in enumerate(self.settings.cloudflare.worker_base_urls, start=1):
                rid = f"worker:{idx}"
                runtime = self.worker_runtime.get(rid, {})
                checked = checks_by_slot.get(idx, {})
                avg_latency_candidates = [
                    x
                    for x in [runtime.get("last_latency_ms"), checked.get("models_latency_ms"), checked.get("generate_latency_ms")]
                    if isinstance(x, (int, float))
                ]
                avg_latency_ms = (
                    round(sum(avg_latency_candidates) / len(avg_latency_candidates), 2) if avg_latency_candidates else None
                )

                items.append(
                    {
                        "slot": idx,
                        "key_mask": "managed-by-worker",
                        "worker_url": worker_url,
                        "is_active": bool(self.worker_rr and self.worker_rr.index == (idx - 1)),
                        "valid": bool(checked.get("models_access_ok") and checked.get("generate_test_ok")) if checked else None,
                        "last_check_at": self.key_health_snapshot.get("last_checked_at"),
                        "last_error": runtime.get("last_error") or checked.get("models_error") or checked.get("generate_error") or "",
                        "models_access_ok": checked.get("models_access_ok"),
                        "generate_test_ok": checked.get("generate_test_ok"),
                        "avg_latency_ms": avg_latency_ms,
                        "success_rate_1h": None,
                        "runtime_success_count": runtime.get("success_count", 0),
                        "runtime_failure_count": runtime.get("failure_count", 0),
                        "runtime_last_status": runtime.get("last_status"),
                        "runtime_last_429_at": runtime.get("last_429_at"),
                    }
                )

            return {
                "last_checked_at": self.key_health_snapshot.get("last_checked_at"),
                "total_keys": len(items),
                "items": items,
                "note": "cloudflare_worker mode: key details are managed inside Worker and are not directly visible.",
            }

        items: List[Dict[str, Any]] = []
        for idx, key in enumerate(self.settings.gemini.api_keys, start=1):
            key_mask = self._mask_key(key)
            rid = f"key:{idx}"
            runtime = self.key_runtime.get(rid, {})
            checked = checks_by_slot.get(idx, {})

            avg_latency_candidates = [
                x
                for x in [runtime.get("last_latency_ms"), checked.get("models_latency_ms"), checked.get("generate_latency_ms")]
                if isinstance(x, (int, float))
            ]
            avg_latency_ms = round(sum(avg_latency_candidates) / len(avg_latency_candidates), 2) if avg_latency_candidates else None

            items.append(
                {
                    "slot": idx,
                    "key_mask": key_mask,
                    "is_active": bool(self.gemini_key_rr and self.gemini_key_rr.index == (idx - 1)),
                    "valid": bool(checked.get("models_access_ok") and checked.get("generate_test_ok")) if checked else None,
                    "last_check_at": self.key_health_snapshot.get("last_checked_at"),
                    "last_error": runtime.get("last_error") or checked.get("models_error") or checked.get("generate_error") or "",
                    "models_access_ok": checked.get("models_access_ok"),
                    "generate_test_ok": checked.get("generate_test_ok"),
                    "avg_latency_ms": avg_latency_ms,
                    "success_rate_1h": self._success_rate_for_key(idx),
                    "runtime_success_count": runtime.get("success_count", 0),
                    "runtime_failure_count": runtime.get("failure_count", 0),
                    "runtime_last_status": runtime.get("last_status"),
                    "runtime_last_429_at": runtime.get("last_429_at"),
                }
            )

        return {
            "last_checked_at": self.key_health_snapshot.get("last_checked_at"),
            "total_keys": len(items),
            "items": items,
            "note": (
                "If proxy mode is cloudflare_worker and Worker holds its own GEMINI_API_KEYS, "
                "these checks only reflect FastAPI-local GEMINI_API_KEYS."
            ),
        }

    def get_rotation_state(self) -> Dict[str, Any]:
        key_slots: List[Dict[str, Any]] = []
        for idx, key in enumerate(self.settings.gemini.api_keys, start=1):
            rid = f"key:{idx}"
            key_slots.append(
                {
                    "slot": idx,
                    "key_mask": self._mask_key(key),
                    "is_active": bool(self.gemini_key_rr and self.gemini_key_rr.index == (idx - 1)),
                    "runtime": self.key_runtime.get(rid, {}),
                }
            )

        worker_slots: List[Dict[str, Any]] = []
        for idx, worker_url in enumerate(self.settings.cloudflare.worker_base_urls, start=1):
            rid = f"worker:{idx}"
            worker_slots.append(
                {
                    "slot": idx,
                    "worker_url": worker_url,
                    "is_active": bool(self.worker_rr and self.worker_rr.index == (idx - 1)),
                    "runtime": self.worker_runtime.get(rid, {}),
                }
            )

        return {
            "mode": self.settings.proxy.mode,
            "key_pool_size": len(key_slots),
            "worker_pool_size": len(worker_slots),
            "active_key_slot": (self.gemini_key_rr.index + 1) if self.gemini_key_rr else None,
            "active_worker_slot": (self.worker_rr.index + 1) if self.worker_rr else None,
            "keys": key_slots,
            "workers": worker_slots,
        }

    def get_recent_usage(self, since_minutes: int = 60) -> Dict[str, Any]:
        since_minutes = max(1, since_minutes)
        cutoff = datetime.now(timezone.utc).timestamp() - since_minutes * 60

        selected: List[Dict[str, Any]] = []
        for rec in self.request_history:
            try:
                ts = datetime.fromisoformat(str(rec.get("ts"))).timestamp()
            except Exception:
                continue
            if ts >= cutoff:
                selected.append(rec)

        by_model: Dict[str, Dict[str, Any]] = {}
        by_key: Dict[str, Dict[str, Any]] = {}
        by_status_class: Dict[str, int] = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "none": 0}

        for rec in selected:
            model = str(rec.get("model") or "unknown")
            key_mask = str(rec.get("key_mask") or "n/a")
            status = rec.get("status_code")
            usage = self._usage_counts(rec.get("usage_metadata") or {})

            bucket = by_model.setdefault(
                model,
                {
                    "requests": 0,
                    "prompt_tokens": 0,
                    "candidate_tokens": 0,
                    "thoughts_tokens": 0,
                    "total_tokens": 0,
                },
            )
            bucket["requests"] += 1
            for key, val in usage.items():
                bucket[key] += val

            kb = by_key.setdefault(
                key_mask,
                {
                    "requests": 0,
                    "prompt_tokens": 0,
                    "candidate_tokens": 0,
                    "thoughts_tokens": 0,
                    "total_tokens": 0,
                },
            )
            kb["requests"] += 1
            for key, val in usage.items():
                kb[key] += val

            if isinstance(status, int):
                cls = f"{status // 100}xx"
                by_status_class[cls] = by_status_class.get(cls, 0) + 1
            else:
                by_status_class["none"] += 1

        return {
            "window_minutes": since_minutes,
            "requests_in_window": len(selected),
            "status_classes": by_status_class,
            "by_model": by_model,
            "by_key": by_key,
            "sample": selected[-50:],
        }

    def get_limits_info(self) -> Dict[str, Any]:
        rate_limited_events = [inc for inc in self.incidents if inc.get("kind") == "rate_limited"]
        return {
            "remaining_credit_per_key_supported": False,
            "remaining_credit_note": (
                "Gemini API does not expose remaining monetary credit per API key via this endpoint. "
                "Quota is typically project-level."
            ),
            "retry_policy": {
                "retry_on_429": self.settings.proxy.retry_on_429,
                "max_retries_per_key": self.settings.proxy.max_retries_per_key,
                "cooloff_sec": self.settings.proxy.cooloff_sec,
            },
            "recent_rate_limited_events": len(rate_limited_events),
            "docs": {
                "all_methods": "https://ai.google.dev/api/all-methods",
                "models": "https://ai.google.dev/api/models#v1beta.models.list",
                "rate_limits": "https://ai.google.dev/gemini-api/docs/rate-limits",
                "pricing": "https://ai.google.dev/gemini-api/docs/pricing",
                "billing": "https://ai.google.dev/gemini-api/docs/billing/",
            },
        }

    async def check_worker_connectivity(self) -> Dict[str, Any]:
        model = self.settings.gemini.default_model
        api_version = self.settings.gemini.api_version
        method = "generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "Reply with: ok"}]}],
            "generationConfig": {"maxOutputTokens": 8, "temperature": 0},
        }

        checks: Dict[str, Any] = {
            "proxy_mode": self.settings.proxy.mode,
            "worker_endpoint": None,
            "reachable": False,
            "status_code": None,
            "classification": "unknown",
            "location": "",
            "detail": "",
        }

        if self.settings.proxy.mode == "cloudflare_worker":
            if not self.worker_rr:
                checks["classification"] = "misconfigured"
                checks["detail"] = "No worker URL configured"
                return checks

            worker_url = self._build_worker_url(api_version=api_version, model=model, method=method)
            checks["worker_endpoint"] = worker_url
            headers = self._build_headers(request_id=None)

            started = time.monotonic()
            try:
                resp = await self.client.post(worker_url, headers=headers, json=payload)
                latency_ms = round((time.monotonic() - started) * 1000.0, 2)
                checks["latency_ms"] = latency_ms
                checks["status_code"] = resp.status_code
                checks["location"] = resp.headers.get("location", "")

                if resp.status_code == 302 and "cloudflareaccess.com" in checks["location"]:
                    checks["classification"] = "cloudflare_access_redirect"
                    checks["detail"] = "Worker is protected by Cloudflare Access and needs service token or policy change."
                elif resp.status_code == 401:
                    checks["classification"] = "worker_auth_failed"
                    checks["detail"] = "Worker auth token/header mismatch"
                elif resp.status_code >= 500:
                    checks["classification"] = "upstream_server_error"
                    checks["detail"] = resp.text[:220]
                else:
                    checks["classification"] = "ok"
                    checks["reachable"] = True
                    checks["detail"] = resp.text[:220]
            except httpx.HTTPError as exc:
                checks["classification"] = "connect_error"
                checks["detail"] = str(exc)
            return checks

        if not self.gemini_key_rr:
            checks["classification"] = "misconfigured"
            checks["detail"] = "No GEMINI_API_KEYS configured for gemini_direct mode"
            return checks

        endpoint = self._build_direct_gemini_url(api_version=api_version, model=model, method=method)
        checks["worker_endpoint"] = endpoint
        headers = self._build_headers(request_id=None)
        started = time.monotonic()
        try:
            resp = await self.client.post(endpoint, headers=headers, json=payload)
            checks["latency_ms"] = round((time.monotonic() - started) * 1000.0, 2)
            checks["status_code"] = resp.status_code
            checks["classification"] = "ok" if resp.status_code < 500 else "upstream_server_error"
            checks["reachable"] = resp.status_code < 500
            checks["detail"] = resp.text[:220]
        except httpx.HTTPError as exc:
            checks["classification"] = "connect_error"
            checks["detail"] = str(exc)
        return checks

    def get_incidents(self, limit: int = 100) -> Dict[str, Any]:
        limit = max(1, min(limit, self.settings.admin.max_incidents))
        items = list(self.incidents)[-limit:]
        return {
            "total": len(self.incidents),
            "returned": len(items),
            "items": items,
        }


class ServiceContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.gemini = GeminiProxyService(settings)

    async def close(self) -> None:
        await self.gemini.aclose()
