#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import string
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

import httpx
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_utc_iso() -> str:
    return now_utc().isoformat()


def parse_iso_utc(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * (p / 100.0)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return values[low]
    weight = rank - low
    return values[low] * (1.0 - weight) + values[high] * weight


def summarize_numbers(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "mean": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "max": None,
            "stdev": None,
        }

    sorted_vals = sorted(values)
    return {
        "count": len(values),
        "min": round(sorted_vals[0], 3),
        "mean": round(statistics.fmean(values), 3),
        "p50": round(percentile(sorted_vals, 50) or 0.0, 3),
        "p90": round(percentile(sorted_vals, 90) or 0.0, 3),
        "p95": round(percentile(sorted_vals, 95) or 0.0, 3),
        "p99": round(percentile(sorted_vals, 99) or 0.0, 3),
        "max": round(sorted_vals[-1], 3),
        "stdev": round(statistics.pstdev(values), 3) if len(values) > 1 else 0.0,
    }


def truncate_text(value: str, max_len: int) -> str:
    value = value or ""
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def classify_status(status_code: Optional[int]) -> str:
    if status_code is None:
        return "none"
    if 200 <= status_code < 300:
        return "2xx"
    if 300 <= status_code < 400:
        return "3xx"
    if 400 <= status_code < 500:
        return "4xx"
    if status_code >= 500:
        return "5xx"
    return "none"


def parse_int_header(value: Any) -> Optional[int]:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def parse_bool_header(value: Any) -> Optional[bool]:
    if value is None:
        return None
    txt = str(value).strip().lower()
    if txt in {"1", "true", "yes", "on"}:
        return True
    if txt in {"0", "false", "no", "off"}:
        return False
    return None


def short_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.1f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    m = int(seconds // 60)
    s = seconds - (m * 60)
    return f"{m}m {s:.1f}s"


class RunConfig(BaseModel):
    name: str = "gemini-proxy-loadtest"
    output_dir: str = "reports/load-tests"
    seed: int = 42
    show_progress_logs: bool = True
    progress_interval_seconds: float = 2.0


class TargetConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8000"
    endpoint: Literal["proxy_gemini", "proxy_gemini_default", "gemini_compatible"] = "proxy_gemini"
    proxy_path: str = "/proxy/gemini"
    proxy_default_path: str = "/proxy/gemini/default"
    compatible_path_template: str = "/{api_version}/models/{model}:{method}"
    request_method: Literal["POST"] = "POST"
    model: str = "gemini-2.5-flash"
    api_version: str = "v1beta"
    method: str = "generateContent"
    verify_tls: bool = True
    default_headers: Dict[str, str] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        return value.rstrip("/")


class LoadProfileConfig(BaseModel):
    total_requests: int = 100
    requests_per_second: float = 5.0
    concurrency: int = 10
    ramp_up_seconds: float = 0.0
    request_timeout_seconds: float = 60.0
    max_connections: int = 100
    max_keepalive_connections: int = 20
    http2: bool = False
    trust_env_proxy: bool = True
    disable_env_proxy_for_loopback: bool = True

    @field_validator("total_requests", "concurrency", "max_connections", "max_keepalive_connections")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be > 0")
        return value

    @field_validator("requests_per_second", "request_timeout_seconds")
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be > 0")
        return value

    @field_validator("ramp_up_seconds")
    @classmethod
    def _non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("must be >= 0")
        return value


class PayloadConfig(BaseModel):
    role: str = "user"
    prompt_templates: List[str] = Field(
        default_factory=lambda: [
            "یک توضیح کوتاه و دقیق درباره امنیت API بنویس.",
            "سه نکته برای بهبود عملکرد سرویس‌های وب بگو.",
            "فرق retry برای 429 و 5xx را فنی و خلاصه توضیح بده.",
        ]
    )
    prompt_min_chars: int = 64
    prompt_max_chars: int = 240
    random_suffix_chars: int = 16
    include_request_metadata_in_prompt: bool = True
    generation_config: Dict[str, Any] = Field(default_factory=lambda: {"temperature": 0.2})
    extra_body: Dict[str, Any] = Field(default_factory=dict)
    send_payload_for_default_endpoint: bool = True

    @field_validator("prompt_templates")
    @classmethod
    def _templates_non_empty(cls, value: List[str]) -> List[str]:
        items = [x for x in value if str(x).strip()]
        if not items:
            raise ValueError("prompt_templates must contain at least one non-empty string")
        return items

    @field_validator("prompt_min_chars", "prompt_max_chars")
    @classmethod
    def _chars_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be >= 0")
        return value

    @field_validator("random_suffix_chars")
    @classmethod
    def _suffix_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be >= 0")
        return value


class AssertionConfig(BaseModel):
    success_status_codes: List[int] = Field(default_factory=lambda: [200])


class AdminConfig(BaseModel):
    enabled: bool = True
    require_auth: bool = False
    header_name: str = "x-admin-token"
    token: str = ""
    usage_window_minutes: int = 120
    incidents_limit: int = 2000
    run_keys_check_before: bool = True
    run_keys_check_after: bool = True
    run_worker_connectivity_before: bool = True
    run_worker_connectivity_after: bool = True
    poll_interval_seconds: float = 0.0


class ReportConfig(BaseModel):
    top_n_slowest: int = 20
    top_n_errors: int = 50
    include_success_samples: int = 10
    max_error_body_chars: int = 500
    top_n_failure_signatures: int = 12


class LoadTestConfig(BaseModel):
    run: RunConfig = Field(default_factory=RunConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    load_profile: LoadProfileConfig = Field(default_factory=LoadProfileConfig)
    payload: PayloadConfig = Field(default_factory=PayloadConfig)
    assertions: AssertionConfig = Field(default_factory=AssertionConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)


@dataclass
class RequestRecord:
    seq: int
    request_id: str
    scheduled_offset_sec: float
    started_at_iso: str
    ended_at_iso: str
    latency_ms: float
    status_code: Optional[int]
    status_class: str
    success: bool
    error_type: Optional[str]
    error_detail: str
    error_status: Optional[str]
    error_code: Optional[int]
    error_message: str
    response_request_id: Optional[str]
    model: str
    endpoint: str
    prompt_len: int
    template_index: int
    response_bytes: int
    response_content_type: str
    proxy_metadata: Dict[str, Any]
    usage_metadata: Dict[str, Any]


class AdminCollector:
    def __init__(self, cfg: LoadTestConfig, client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = client

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.cfg.admin.require_auth and self.cfg.admin.token:
            headers[self.cfg.admin.header_name] = self.cfg.admin.token
        elif self.cfg.admin.token:
            headers[self.cfg.admin.header_name] = self.cfg.admin.token
        return headers

    async def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        url = f"{self.cfg.target.base_url}{path}"
        try:
            resp = await self.client.get(url, headers=self._headers(), params=params)
        except Exception as exc:
            return None, f"GET {path} failed: {exc}"

        if "application/json" not in resp.headers.get("content-type", "").lower():
            return None, f"GET {path} returned non-json status={resp.status_code}"
        try:
            data = resp.json()
        except Exception as exc:
            return None, f"GET {path} invalid json: {exc}"

        if resp.status_code >= 400:
            return None, f"GET {path} status={resp.status_code} body={truncate_text(resp.text, 260)}"
        if not isinstance(data, dict):
            return None, f"GET {path} returned json but not object"
        return data, None

    async def _post_json(self, path: str, json_body: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        url = f"{self.cfg.target.base_url}{path}"
        try:
            resp = await self.client.post(url, headers=self._headers(), json=json_body or {})
        except Exception as exc:
            return None, f"POST {path} failed: {exc}"

        if "application/json" not in resp.headers.get("content-type", "").lower():
            return None, f"POST {path} returned non-json status={resp.status_code}"
        try:
            data = resp.json()
        except Exception as exc:
            return None, f"POST {path} invalid json: {exc}"

        if resp.status_code >= 400:
            return None, f"POST {path} status={resp.status_code} body={truncate_text(resp.text, 260)}"
        if not isinstance(data, dict):
            return None, f"POST {path} returned json but not object"
        return data, None

    async def snapshot(self, stage: str, run_started_at: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "stage": stage,
            "captured_at": now_utc_iso(),
            "run_started_at": run_started_at,
            "errors": [],
        }

        health, err = await self._get_json("/health")
        if err:
            out["errors"].append(err)
        out["health"] = health

        rotation, err = await self._get_json("/admin/keys/rotation-state")
        if err:
            out["errors"].append(err)
        out["rotation_state"] = rotation

        keys_status, err = await self._get_json("/admin/keys/status")
        if err:
            out["errors"].append(err)
        out["keys_status"] = keys_status

        usage_recent, err = await self._get_json(
            "/admin/usage/recent",
            params={"since_minutes": self.cfg.admin.usage_window_minutes},
        )
        if err:
            out["errors"].append(err)
        out["usage_recent"] = usage_recent

        incidents, err = await self._get_json(
            "/admin/incidents",
            params={"limit": self.cfg.admin.incidents_limit},
        )
        if err:
            out["errors"].append(err)
        out["incidents"] = incidents

        if (stage == "before" and self.cfg.admin.run_worker_connectivity_before) or (
            stage == "after" and self.cfg.admin.run_worker_connectivity_after
        ):
            connectivity, err = await self._get_json("/admin/worker/connectivity")
            if err:
                out["errors"].append(err)
            out["worker_connectivity"] = connectivity

        if (stage == "before" and self.cfg.admin.run_keys_check_before) or (
            stage == "after" and self.cfg.admin.run_keys_check_after
        ):
            keys_check, err = await self._post_json("/admin/keys/check")
            if err:
                out["errors"].append(err)
            out["keys_check"] = keys_check

        return out


class LoadRunner:
    def __init__(self, cfg: LoadTestConfig):
        self.cfg = cfg
        self.rand = random.Random(cfg.run.seed)
        self.run_id = f"{cfg.run.name}-{now_utc().strftime('%Y%m%d-%H%M%S')}-{self.rand.randrange(1000, 9999)}"

    def _choose_template(self, seq: int) -> Tuple[int, str]:
        idx = seq % len(self.cfg.payload.prompt_templates)
        return idx, self.cfg.payload.prompt_templates[idx]

    def _build_prompt(self, seq: int) -> Tuple[int, str]:
        template_idx, template = self._choose_template(seq)
        text = template

        if self.cfg.payload.include_request_metadata_in_prompt:
            text = f"{text}\n[run_id={self.run_id} seq={seq}]"

        letters = string.ascii_lowercase + string.digits
        if self.cfg.payload.random_suffix_chars > 0:
            suffix = "".join(self.rand.choice(letters) for _ in range(self.cfg.payload.random_suffix_chars))
            text = f"{text}\nnonce={suffix}"

        min_chars = max(0, self.cfg.payload.prompt_min_chars)
        max_chars = max(0, self.cfg.payload.prompt_max_chars)

        filler = " این جمله برای رسیدن به حداقل طول پرامپت اضافه شده است."
        while min_chars > 0 and len(text) < min_chars:
            text += filler

        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars]

        return template_idx, text

    def _build_path(self) -> str:
        t = self.cfg.target
        if t.endpoint == "proxy_gemini":
            return t.proxy_path
        if t.endpoint == "proxy_gemini_default":
            return t.proxy_default_path
        return t.compatible_path_template.format(api_version=t.api_version, model=t.model, method=t.method)

    def _build_body(self, prompt: str) -> Dict[str, Any]:
        t = self.cfg.target
        p = self.cfg.payload

        if t.endpoint == "proxy_gemini_default" and not p.send_payload_for_default_endpoint:
            return {}

        body: Dict[str, Any] = {
            "contents": [{"role": p.role, "parts": [{"text": prompt}]}],
        }

        if t.endpoint in {"proxy_gemini", "proxy_gemini_default"}:
            body["model"] = t.model
            body["api_version"] = t.api_version
            body["method"] = t.method

        if p.generation_config:
            body["generationConfig"] = p.generation_config

        if p.extra_body:
            body = deep_merge(body, p.extra_body)

        return body

    def _schedule_offset(self, seq: int) -> float:
        lp = self.cfg.load_profile
        k = seq + 1
        rps = lp.requests_per_second
        ramp = lp.ramp_up_seconds
        if ramp <= 0:
            return k / rps

        ramp_capacity = 0.5 * rps * ramp
        if ramp_capacity > 0 and k <= ramp_capacity:
            return math.sqrt((2.0 * k * ramp) / rps)

        return ramp + ((k - ramp_capacity) / rps)

    @staticmethod
    def _extract_usage_metadata(json_payload: Any) -> Dict[str, Any]:
        if not isinstance(json_payload, dict):
            return {}
        usage = json_payload.get("usageMetadata")
        return usage if isinstance(usage, dict) else {}

    @staticmethod
    def _extract_proxy_metadata(json_payload: Any, headers: httpx.Headers) -> Dict[str, Any]:
        from_body: Dict[str, Any] = {}
        if isinstance(json_payload, dict) and isinstance(json_payload.get("proxy_metadata"), dict):
            from_body = dict(json_payload["proxy_metadata"])

        from_headers: Dict[str, Any] = {
            "served_via": headers.get("x-proxy-served-via"),
            "key_slot": parse_int_header(headers.get("x-proxy-key-slot")),
            "key_pool_size": parse_int_header(headers.get("x-proxy-key-pool-size")),
            "worker_slot": parse_int_header(headers.get("x-proxy-worker-slot")),
            "worker_url": headers.get("x-proxy-worker-url"),
            "attempts": parse_int_header(headers.get("x-proxy-attempts")),
            "key_rotated": parse_bool_header(headers.get("x-proxy-key-rotated")),
        }

        merged = dict(from_headers)
        merged.update({k: v for k, v in from_body.items() if v is not None})

        if merged.get("served_via") is None:
            merged["served_via"] = "unknown"
        if merged.get("key_rotated") is None:
            merged["key_rotated"] = False

        return merged

    @staticmethod
    def _extract_error_type(json_payload: Any, status_code: Optional[int], transport_error: Optional[str]) -> Optional[str]:
        if transport_error:
            return "transport_error"
        if isinstance(json_payload, dict):
            err = json_payload.get("error")
            if isinstance(err, dict):
                status = err.get("status")
                if status:
                    return str(status)
                message = err.get("message")
                if message:
                    return "upstream_error"
        if status_code is not None and status_code >= 500:
            return "http_5xx"
        if status_code is not None and status_code == 429:
            return "http_429"
        if status_code is not None and status_code >= 400:
            return "http_4xx"
        return None

    async def _run_admin_poller(
        self,
        collector: AdminCollector,
        run_started_at: str,
        stop_event: asyncio.Event,
        snapshots: List[Dict[str, Any]],
    ) -> None:
        interval = self.cfg.admin.poll_interval_seconds
        if interval <= 0:
            return

        while not stop_event.is_set():
            await asyncio.sleep(interval)
            if stop_event.is_set():
                break
            snap = await collector.snapshot(stage="poll", run_started_at=run_started_at)
            snapshots.append(snap)

    @staticmethod
    def _log_stage(message: str) -> None:
        print(f"[{now_utc_iso()}] {message}", flush=True)

    async def _run_progress_reporter(
        self,
        *,
        total: int,
        stats: Dict[str, int],
        run_start_monotonic: float,
        stop_event: asyncio.Event,
        interval_sec: float,
    ) -> None:
        if interval_sec <= 0:
            return

        last_completed = 0
        last_tick = time.monotonic()
        while not stop_event.is_set():
            await asyncio.sleep(interval_sec)
            if stop_event.is_set():
                break

            now_tick = time.monotonic()
            completed = stats.get("completed", 0)
            delta = max(0, completed - last_completed)
            window_sec = max(0.001, now_tick - last_tick)
            instant_rps = delta / window_sec

            elapsed = max(0.001, now_tick - run_start_monotonic)
            avg_rps = completed / elapsed
            success = stats.get("success", 0)
            failure = stats.get("failure", 0)
            pct = (completed / total) * 100.0 if total > 0 else 0.0

            self._log_stage(
                (
                    f"progress {completed}/{total} ({pct:.1f}%) "
                    f"success={success} failure={failure} "
                    f"rps_now={instant_rps:.2f} rps_avg={avg_rps:.2f}"
                )
            )
            last_completed = completed
            last_tick = now_tick

    async def run(self) -> Dict[str, Any]:
        cfg = self.cfg
        lp = cfg.load_profile
        success_codes = set(cfg.assertions.success_status_codes)

        output_dir = Path(cfg.run.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        run_started_at_dt = now_utc()
        run_started_at = run_started_at_dt.isoformat()

        shared_limits = httpx.Limits(
            max_connections=lp.max_connections,
            max_keepalive_connections=lp.max_keepalive_connections,
        )

        request_records: List[RequestRecord] = []
        all_errors: List[str] = []
        live_stats: Dict[str, int] = {"completed": 0, "success": 0, "failure": 0, "transport_errors": 0}

        trust_env_proxy = bool(lp.trust_env_proxy)
        target_host = (urlparse(cfg.target.base_url).hostname or "").strip().lower()
        loopback_hosts = {"localhost", "127.0.0.1", "::1"}
        if trust_env_proxy and lp.disable_env_proxy_for_loopback and target_host in loopback_hosts:
            trust_env_proxy = False
            self._log_stage(
                f"detected loopback target ({target_host}), forcing trust_env_proxy=false to avoid local proxy interference"
            )
        if trust_env_proxy:
            # Keep parity with app runtime: httpx rejects socks:// in ALL_PROXY for this setup.
            for key in ("ALL_PROXY", "all_proxy"):
                val = os.getenv(key, "")
                if val.lower().startswith("socks://"):
                    os.environ.pop(key, None)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(lp.request_timeout_seconds),
            verify=cfg.target.verify_tls,
            limits=shared_limits,
            http2=lp.http2,
            trust_env=trust_env_proxy,
            headers=cfg.target.default_headers,
        ) as client:
            collector = AdminCollector(cfg, client)

            admin_before: Optional[Dict[str, Any]] = None
            admin_after: Optional[Dict[str, Any]] = None
            admin_poll_samples: List[Dict[str, Any]] = []

            if cfg.run.show_progress_logs:
                self._log_stage(f"run_id={self.run_id} load test started")

            if cfg.admin.enabled:
                if cfg.run.show_progress_logs:
                    self._log_stage("collecting admin snapshot: before")
                admin_before = await collector.snapshot(stage="before", run_started_at=run_started_at)
                if cfg.run.show_progress_logs:
                    self._log_stage("admin snapshot before collected")

            stop_event = asyncio.Event()
            poller_task: Optional[asyncio.Task[Any]] = None
            if cfg.admin.enabled and cfg.admin.poll_interval_seconds > 0:
                poller_task = asyncio.create_task(
                    self._run_admin_poller(collector, run_started_at, stop_event, admin_poll_samples)
                )

            sem = asyncio.Semaphore(lp.concurrency)
            run_start_monotonic = time.monotonic()
            path = self._build_path()

            progress_task: Optional[asyncio.Task[Any]] = None
            if cfg.run.show_progress_logs:
                self._log_stage(
                    (
                        f"sending traffic total={lp.total_requests} rps={lp.requests_per_second} "
                        f"concurrency={lp.concurrency} endpoint={cfg.target.endpoint}"
                    )
                )
                progress_task = asyncio.create_task(
                    self._run_progress_reporter(
                        total=lp.total_requests,
                        stats=live_stats,
                        run_start_monotonic=run_start_monotonic,
                        stop_event=stop_event,
                        interval_sec=max(0.1, float(cfg.run.progress_interval_seconds)),
                    )
                )

            async def one_request(seq: int) -> None:
                await sem.acquire()
                try:
                    scheduled_offset = self._schedule_offset(seq)
                    scheduled_target = run_start_monotonic + scheduled_offset
                    now_monotonic = time.monotonic()
                    if scheduled_target > now_monotonic:
                        await asyncio.sleep(scheduled_target - now_monotonic)

                    template_idx, prompt = self._build_prompt(seq)
                    body = self._build_body(prompt)
                    req_id = f"{self.run_id}-r{seq:06d}"
                    url = f"{cfg.target.base_url}{path}"
                    headers = dict(cfg.target.default_headers)
                    headers["x-request-id"] = req_id

                    started_at = now_utc_iso()
                    started = time.monotonic()

                    status_code: Optional[int] = None
                    status_class = "none"
                    content_type = ""
                    error_detail = ""
                    response_bytes = 0
                    response_json: Any = None
                    proxy_meta: Dict[str, Any] = {}
                    usage_meta: Dict[str, Any] = {}
                    transport_error: Optional[str] = None
                    error_status: Optional[str] = None
                    error_code: Optional[int] = None
                    error_message: str = ""
                    response_request_id: Optional[str] = None

                    try:
                        resp = await client.request(
                            cfg.target.request_method,
                            url,
                            headers=headers,
                            json=body,
                        )
                        status_code = resp.status_code
                        status_class = classify_status(status_code)
                        content_type = resp.headers.get("content-type", "")
                        response_bytes = len(resp.content or b"")
                        response_request_id = str(resp.headers.get("x-request-id") or "").strip() or None

                        if "application/json" in content_type.lower():
                            try:
                                response_json = resp.json()
                            except Exception:
                                response_json = None

                        if isinstance(response_json, dict):
                            err_obj = response_json.get("error")
                            if isinstance(err_obj, dict):
                                raw_status = err_obj.get("status")
                                if raw_status is not None:
                                    error_status = str(raw_status)
                                raw_code = err_obj.get("code")
                                if isinstance(raw_code, int):
                                    error_code = raw_code
                                elif raw_code is not None:
                                    parsed_code = to_int(raw_code, default=0)
                                    error_code = parsed_code if parsed_code > 0 else None
                                raw_message = err_obj.get("message")
                                if raw_message is not None:
                                    error_message = str(raw_message)
                            if not error_message and response_json.get("detail") is not None:
                                error_message = str(response_json.get("detail"))

                        proxy_meta = self._extract_proxy_metadata(response_json, resp.headers)
                        usage_meta = self._extract_usage_metadata(response_json)

                        if status_code >= 400:
                            raw_detail = resp.text or ""
                            if not raw_detail and error_message:
                                raw_detail = error_message
                            error_detail = truncate_text(raw_detail, cfg.report.max_error_body_chars)
                    except Exception as exc:
                        transport_error = str(exc)
                        error_detail = truncate_text(str(exc), cfg.report.max_error_body_chars)

                    ended = time.monotonic()
                    ended_at = now_utc_iso()
                    latency_ms = round((ended - started) * 1000.0, 3)

                    error_type = self._extract_error_type(response_json, status_code, transport_error)
                    success = bool(status_code in success_codes and transport_error is None)

                    request_records.append(
                        RequestRecord(
                            seq=seq,
                            request_id=req_id,
                            scheduled_offset_sec=round(scheduled_offset, 6),
                            started_at_iso=started_at,
                            ended_at_iso=ended_at,
                            latency_ms=latency_ms,
                            status_code=status_code,
                            status_class=status_class,
                            success=success,
                            error_type=error_type,
                            error_detail=error_detail,
                            error_status=error_status,
                            error_code=error_code,
                            error_message=error_message,
                            response_request_id=response_request_id,
                            model=cfg.target.model,
                            endpoint=cfg.target.endpoint,
                            prompt_len=len(prompt),
                            template_index=template_idx,
                            response_bytes=response_bytes,
                            response_content_type=content_type,
                            proxy_metadata=proxy_meta,
                            usage_metadata=usage_meta,
                        )
                    )
                    live_stats["completed"] += 1
                    if success:
                        live_stats["success"] += 1
                    else:
                        live_stats["failure"] += 1
                    if transport_error is not None:
                        live_stats["transport_errors"] += 1
                finally:
                    sem.release()

            tasks = [asyncio.create_task(one_request(seq)) for seq in range(lp.total_requests)]
            await asyncio.gather(*tasks)
            run_end_monotonic = time.monotonic()

            if cfg.run.show_progress_logs:
                self._log_stage("traffic phase completed")

            if cfg.admin.enabled:
                if cfg.run.show_progress_logs:
                    self._log_stage("collecting admin snapshot: after")
                admin_after = await collector.snapshot(stage="after", run_started_at=run_started_at)
                if cfg.run.show_progress_logs:
                    self._log_stage("admin snapshot after collected")

            stop_event.set()
            if progress_task is not None:
                await progress_task
            if poller_task is not None:
                await poller_task

        run_ended_at_dt = now_utc()
        run_ended_at = run_ended_at_dt.isoformat()
        total_duration_sec = max(0.001, run_end_monotonic - run_start_monotonic)

        if not request_records:
            raise RuntimeError("No request records were captured.")

        records_sorted = sorted(request_records, key=lambda x: x.seq)

        latency_values = [r.latency_ms for r in records_sorted]
        response_bytes = [float(r.response_bytes) for r in records_sorted]
        prompt_lengths = [float(r.prompt_len) for r in records_sorted]

        status_counts = Counter(str(r.status_code) if r.status_code is not None else "none" for r in records_sorted)
        status_class_counts = Counter(r.status_class for r in records_sorted)
        error_type_counts = Counter(r.error_type or "none" for r in records_sorted)

        served_via_counts = Counter(str((r.proxy_metadata or {}).get("served_via") or "unknown") for r in records_sorted)

        key_slot_counts = Counter(
            str((r.proxy_metadata or {}).get("key_slot"))
            for r in records_sorted
            if (r.proxy_metadata or {}).get("key_slot") is not None
        )
        worker_slot_counts = Counter(
            str((r.proxy_metadata or {}).get("worker_slot"))
            for r in records_sorted
            if (r.proxy_metadata or {}).get("worker_slot") is not None
        )
        worker_url_counts = Counter(
            str((r.proxy_metadata or {}).get("worker_url"))
            for r in records_sorted
            if (r.proxy_metadata or {}).get("worker_url")
        )

        key_rotated_true = sum(1 for r in records_sorted if bool((r.proxy_metadata or {}).get("key_rotated")))
        attempts_values = [
            to_int((r.proxy_metadata or {}).get("attempts"), default=1)
            for r in records_sorted
            if (r.proxy_metadata or {}).get("attempts") is not None
        ]
        attempts_gt1 = sum(1 for a in attempts_values if a > 1)
        estimated_retry_attempts = sum(max(0, a - 1) for a in attempts_values)

        key_slots_ordered = [
            to_int((r.proxy_metadata or {}).get("key_slot"), default=0)
            for r in records_sorted
            if (r.proxy_metadata or {}).get("key_slot") is not None
        ]
        observed_key_slot_switches = 0
        for i in range(1, len(key_slots_ordered)):
            if key_slots_ordered[i] != key_slots_ordered[i - 1]:
                observed_key_slot_switches += 1

        worker_slots_ordered = [
            to_int((r.proxy_metadata or {}).get("worker_slot"), default=0)
            for r in records_sorted
            if (r.proxy_metadata or {}).get("worker_slot") is not None
        ]
        observed_worker_slot_switches = 0
        for i in range(1, len(worker_slots_ordered)):
            if worker_slots_ordered[i] != worker_slots_ordered[i - 1]:
                observed_worker_slot_switches += 1

        success_count = sum(1 for r in records_sorted if r.success)
        failure_count = len(records_sorted) - success_count
        transport_error_count = sum(1 for r in records_sorted if r.error_type == "transport_error")

        usage_totals = {
            "prompt_tokens": 0,
            "candidate_tokens": 0,
            "thoughts_tokens": 0,
            "total_tokens": 0,
        }
        for r in records_sorted:
            usage = r.usage_metadata or {}
            usage_totals["prompt_tokens"] += to_int(usage.get("promptTokenCount"), 0)
            usage_totals["candidate_tokens"] += to_int(usage.get("candidatesTokenCount"), 0)
            usage_totals["thoughts_tokens"] += to_int(usage.get("thoughtsTokenCount"), 0)
            usage_totals["total_tokens"] += to_int(usage.get("totalTokenCount"), 0)

        by_worker_slot: Dict[str, Dict[str, Any]] = {}
        by_key_slot: Dict[str, Dict[str, Any]] = {}

        worker_latency_buckets: Dict[str, List[float]] = defaultdict(list)
        key_latency_buckets: Dict[str, List[float]] = defaultdict(list)

        for r in records_sorted:
            worker_slot = (r.proxy_metadata or {}).get("worker_slot")
            key_slot = (r.proxy_metadata or {}).get("key_slot")

            if worker_slot is not None:
                wkey = str(worker_slot)
                bucket = by_worker_slot.setdefault(
                    wkey,
                    {
                        "requests": 0,
                        "success": 0,
                        "failed": 0,
                        "status_429": 0,
                        "status_5xx": 0,
                    },
                )
                bucket["requests"] += 1
                if r.success:
                    bucket["success"] += 1
                else:
                    bucket["failed"] += 1
                if r.status_code == 429:
                    bucket["status_429"] += 1
                if (r.status_code or 0) >= 500:
                    bucket["status_5xx"] += 1
                worker_latency_buckets[wkey].append(r.latency_ms)

            if key_slot is not None:
                kkey = str(key_slot)
                bucket = by_key_slot.setdefault(
                    kkey,
                    {
                        "requests": 0,
                        "success": 0,
                        "failed": 0,
                        "status_429": 0,
                        "status_5xx": 0,
                    },
                )
                bucket["requests"] += 1
                if r.success:
                    bucket["success"] += 1
                else:
                    bucket["failed"] += 1
                if r.status_code == 429:
                    bucket["status_429"] += 1
                if (r.status_code or 0) >= 500:
                    bucket["status_5xx"] += 1
                key_latency_buckets[kkey].append(r.latency_ms)

        for slot, latencies in worker_latency_buckets.items():
            by_worker_slot[slot]["latency_ms"] = summarize_numbers(latencies)

        for slot, latencies in key_latency_buckets.items():
            by_key_slot[slot]["latency_ms"] = summarize_numbers(latencies)

        slowest = sorted(records_sorted, key=lambda x: x.latency_ms, reverse=True)[: cfg.report.top_n_slowest]
        errors = [r for r in records_sorted if not r.success][: cfg.report.top_n_errors]
        success_samples = [r for r in records_sorted if r.success][: cfg.report.include_success_samples]

        admin_delta = build_admin_delta(
            before=admin_before,
            after=admin_after,
            run_started_at=run_started_at,
        )
        failure_analysis = build_failure_analysis(
            records=records_sorted,
            admin_after=admin_after,
            run_started_at=run_started_at,
            top_n_signatures=cfg.report.top_n_failure_signatures,
        )

        report: Dict[str, Any] = {
            "meta": {
                "run_id": self.run_id,
                "started_at": run_started_at,
                "ended_at": run_ended_at,
                "duration_seconds": round(total_duration_sec, 3),
                "timezone": "UTC",
            },
            "target": cfg.target.model_dump(),
            "resolved_config": cfg.model_dump(),
            "summary": {
                "total_requests": len(records_sorted),
                "success_count": success_count,
                "failure_count": failure_count,
                "success_rate": round((success_count / len(records_sorted)) * 100.0, 2),
                "transport_error_count": transport_error_count,
                "planned_rps": lp.requests_per_second,
                "achieved_rps": round(len(records_sorted) / total_duration_sec, 3),
                "status_counts": dict(status_counts),
                "status_class_counts": dict(status_class_counts),
                "error_type_counts": dict(error_type_counts),
            },
            "latency_ms": summarize_numbers(latency_values),
            "response_bytes": summarize_numbers(response_bytes),
            "prompt_length": summarize_numbers(prompt_lengths),
            "proxy_observability": {
                "served_via_counts": dict(served_via_counts),
                "key_slot_counts": dict(key_slot_counts),
                "worker_slot_counts": dict(worker_slot_counts),
                "worker_url_counts": dict(worker_url_counts),
                "key_rotated_true_count": key_rotated_true,
                "attempts_distribution": dict(Counter(attempts_values)),
                "attempts_gt1_count": attempts_gt1,
                "estimated_retry_attempts": estimated_retry_attempts,
                "observed_key_slot_switches": observed_key_slot_switches,
                "observed_worker_slot_switches": observed_worker_slot_switches,
                "by_worker_slot": by_worker_slot,
                "by_key_slot": by_key_slot,
            },
            "usage_tokens_total": usage_totals,
            "admin_telemetry": {
                "enabled": cfg.admin.enabled,
                "before": admin_before,
                "after": admin_after,
                "poll_samples": admin_poll_samples,
                "delta": admin_delta,
            },
            "failure_analysis": failure_analysis,
            "samples": {
                "slowest_requests": [record_to_dict(r) for r in slowest],
                "error_requests": [record_to_dict(r) for r in errors],
                "success_samples": [record_to_dict(r) for r in success_samples],
            },
            "warnings": all_errors,
        }

        report_path = output_dir / f"{self.run_id}.json"
        with report_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        print_cli_summary(report, report_path)
        return report


def record_to_dict(r: RequestRecord) -> Dict[str, Any]:
    return {
        "seq": r.seq,
        "request_id": r.request_id,
        "scheduled_offset_sec": r.scheduled_offset_sec,
        "started_at": r.started_at_iso,
        "ended_at": r.ended_at_iso,
        "latency_ms": r.latency_ms,
        "status_code": r.status_code,
        "status_class": r.status_class,
        "success": r.success,
        "error_type": r.error_type,
        "error_detail": r.error_detail,
        "error_status": r.error_status,
        "error_code": r.error_code,
        "error_message": r.error_message,
        "response_request_id": r.response_request_id,
        "model": r.model,
        "endpoint": r.endpoint,
        "prompt_len": r.prompt_len,
        "template_index": r.template_index,
        "response_bytes": r.response_bytes,
        "response_content_type": r.response_content_type,
        "proxy_metadata": r.proxy_metadata,
        "usage_metadata": r.usage_metadata,
    }


def _extract_incident_index(admin_after: Optional[Dict[str, Any]], run_started_at: str) -> Tuple[Dict[str, List[Dict[str, Any]]], Counter]:
    by_request: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    kind_counter: Counter = Counter()
    if not admin_after:
        return by_request, kind_counter

    run_start_dt = parse_iso_utc(run_started_at)
    incidents = ((admin_after.get("incidents") or {}).get("items") or []) if isinstance(admin_after, dict) else []
    for inc in incidents:
        if not isinstance(inc, dict):
            continue
        ts = parse_iso_utc(str(inc.get("ts") or ""))
        if run_start_dt is not None and ts is not None and ts < run_start_dt:
            continue
        kind = str(inc.get("kind") or "unknown")
        kind_counter[kind] += 1
        ctx = inc.get("context") or {}
        rid = str(ctx.get("request_id") or "").strip()
        if rid:
            by_request[rid].append(inc)
    return by_request, kind_counter


def _infer_failure_location_and_cause(record: RequestRecord) -> Dict[str, str]:
    status = record.status_code
    served_via = str((record.proxy_metadata or {}).get("served_via") or "unknown").strip().lower()
    error_status = str(record.error_status or "").strip().upper()
    detail_l = " ".join(
        [
            str(record.error_detail or "").lower(),
            str(record.error_message or "").lower(),
            str(record.error_type or "").lower(),
        ]
    )

    if record.error_type == "transport_error":
        location = "load_test_client_or_network"
        if "timed out" in detail_l or "timeout" in detail_l:
            cause = "client_timeout"
        elif "connection" in detail_l or "connect" in detail_l:
            cause = "connection_error"
        else:
            cause = "transport_error"
        return {"location": location, "cause": cause}

    if served_via == "cloudflare_worker":
        location = "worker_path_or_upstream_gemini"
        if status == 429 or error_status == "RESOURCE_EXHAUSTED":
            cause = "upstream_rate_limited"
        elif status is not None and status >= 500:
            cause = "upstream_5xx_via_worker"
        elif status in {401, 403}:
            cause = "worker_auth_or_access_policy"
        elif status is not None and status >= 400:
            cause = "worker_or_upstream_4xx"
        else:
            cause = "unknown_worker_path_failure"
        return {"location": location, "cause": cause}

    if served_via == "gemini_direct":
        location = "direct_gemini_upstream"
        if status == 429 or error_status == "RESOURCE_EXHAUSTED":
            cause = "gemini_rate_limited"
        elif status is not None and status >= 500:
            cause = "gemini_5xx"
        elif status in {401, 403}:
            cause = "gemini_auth_failure"
        elif status is not None and status >= 400:
            cause = "gemini_4xx"
        else:
            cause = "unknown_gemini_direct_failure"
        return {"location": location, "cause": cause}

    location = "fastapi_gateway_or_local_proxy"
    if status == 502 and ("upstream connection error" in detail_l or "connection error" in detail_l):
        cause = "gateway_upstream_connect_error"
    elif status == 503:
        cause = "gateway_unavailable_or_misconfigured"
    elif status is not None and status >= 500:
        cause = "gateway_5xx_before_proxy_metadata"
    elif status in {401, 403}:
        cause = "gateway_auth_or_access"
    elif status == 422:
        cause = "request_validation_error"
    elif status is not None and status >= 400:
        cause = "gateway_4xx"
    else:
        cause = "unknown_gateway_failure"
    return {"location": location, "cause": cause}


def build_failure_analysis(
    *,
    records: List[RequestRecord],
    admin_after: Optional[Dict[str, Any]],
    run_started_at: str,
    top_n_signatures: int = 12,
) -> Dict[str, Any]:
    failures = [r for r in records if not r.success]
    if not failures:
        return {
            "total_failures": 0,
            "summary": "no failures",
            "by_location": {},
            "by_cause": {},
            "by_status_code": {},
            "by_error_status": {},
            "correlated_incident_kinds": {},
            "top_signatures": [],
            "items": [],
        }

    incident_by_request, _ = _extract_incident_index(admin_after=admin_after, run_started_at=run_started_at)

    by_location: Counter = Counter()
    by_cause: Counter = Counter()
    by_status_code: Counter = Counter()
    by_error_status: Counter = Counter()
    by_incident_kind: Counter = Counter()
    signature_counter: Counter = Counter()
    signature_samples: Dict[str, List[str]] = defaultdict(list)
    items: List[Dict[str, Any]] = []

    for rec in failures:
        loc_cause = _infer_failure_location_and_cause(rec)
        location = loc_cause["location"]
        cause = loc_cause["cause"]

        status_key = str(rec.status_code) if rec.status_code is not None else "none"
        err_status_key = str(rec.error_status or "none")

        matched = incident_by_request.get(rec.request_id, [])
        matched_kinds = [str((x or {}).get("kind") or "unknown") for x in matched]
        for kind in matched_kinds:
            by_incident_kind[kind] += 1

        by_location[location] += 1
        by_cause[cause] += 1
        by_status_code[status_key] += 1
        by_error_status[err_status_key] += 1

        signature = f"{location}|{cause}|http={status_key}|gemini={err_status_key}"
        signature_counter[signature] += 1
        if len(signature_samples[signature]) < 5:
            signature_samples[signature].append(rec.request_id)

        items.append(
            {
                "request_id": rec.request_id,
                "response_request_id": rec.response_request_id,
                "seq": rec.seq,
                "status_code": rec.status_code,
                "error_type": rec.error_type,
                "error_status": rec.error_status,
                "error_code": rec.error_code,
                "error_message": rec.error_message,
                "error_detail": rec.error_detail,
                "location": location,
                "cause": cause,
                "proxy_metadata": rec.proxy_metadata,
                "matched_incident_kinds": matched_kinds,
            }
        )

    top_sigs: List[Dict[str, Any]] = []
    for signature, count in signature_counter.most_common(max(1, top_n_signatures)):
        top_sigs.append(
            {
                "signature": signature,
                "count": count,
                "sample_request_ids": signature_samples.get(signature, [])[:5],
            }
        )

    return {
        "total_failures": len(failures),
        "by_location": dict(by_location),
        "by_cause": dict(by_cause),
        "by_status_code": dict(by_status_code),
        "by_error_status": dict(by_error_status),
        "correlated_incident_kinds": dict(by_incident_kind),
        "top_signatures": top_sigs,
        "items": items,
    }


def build_runtime_delta(before_items: List[Dict[str, Any]], after_items: List[Dict[str, Any]], kind: str) -> Dict[str, Any]:
    def index_items(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for item in items:
            slot = str(item.get("slot"))
            runtime = item.get("runtime") or {}
            out[slot] = {
                "slot": item.get("slot"),
                "key_mask": item.get("key_mask"),
                "worker_url": item.get("worker_url"),
                "success_count": to_int(runtime.get("success_count"), 0),
                "failure_count": to_int(runtime.get("failure_count"), 0),
                "last_429_at": runtime.get("last_429_at"),
                "last_status": runtime.get("last_status"),
                "last_latency_ms": runtime.get("last_latency_ms"),
            }
        return out

    b = index_items(before_items)
    a = index_items(after_items)
    slots = sorted(set(b.keys()) | set(a.keys()), key=lambda x: int(x) if x.isdigit() else x)

    deltas: List[Dict[str, Any]] = []
    total_success_delta = 0
    total_failure_delta = 0

    for slot in slots:
        bi = b.get(slot, {})
        ai = a.get(slot, {})

        success_delta = to_int(ai.get("success_count"), 0) - to_int(bi.get("success_count"), 0)
        failure_delta = to_int(ai.get("failure_count"), 0) - to_int(bi.get("failure_count"), 0)

        total_success_delta += success_delta
        total_failure_delta += failure_delta

        deltas.append(
            {
                "slot": to_int(slot, 0),
                "key_mask": ai.get("key_mask") or bi.get("key_mask"),
                "worker_url": ai.get("worker_url") or bi.get("worker_url"),
                "success_count_delta": success_delta,
                "failure_count_delta": failure_delta,
                "last_429_at_after": ai.get("last_429_at"),
                "last_status_after": ai.get("last_status"),
                "last_latency_ms_after": ai.get("last_latency_ms"),
            }
        )

    return {
        "kind": kind,
        "total_success_delta": total_success_delta,
        "total_failure_delta": total_failure_delta,
        "slots": deltas,
    }


def build_admin_delta(
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
    run_started_at: str,
) -> Dict[str, Any]:
    if not before or not after:
        return {"available": False, "reason": "admin snapshots unavailable"}

    before_rotation = (before.get("rotation_state") or {}) if isinstance(before, dict) else {}
    after_rotation = (after.get("rotation_state") or {}) if isinstance(after, dict) else {}

    worker_delta = build_runtime_delta(
        before_rotation.get("workers") or [],
        after_rotation.get("workers") or [],
        kind="workers",
    )
    key_delta = build_runtime_delta(
        before_rotation.get("keys") or [],
        after_rotation.get("keys") or [],
        kind="keys",
    )

    before_inc = before.get("incidents") or {}
    after_inc = after.get("incidents") or {}

    before_total = to_int(before_inc.get("total"), 0)
    after_total = to_int(after_inc.get("total"), 0)
    incidents_total_delta = max(0, after_total - before_total)

    run_start_dt = parse_iso_utc(run_started_at)
    new_incidents: List[Dict[str, Any]] = []
    for item in (after_inc.get("items") or []):
        if not isinstance(item, dict):
            continue
        ts = parse_iso_utc(str(item.get("ts") or ""))
        if run_start_dt is not None and ts is not None and ts >= run_start_dt:
            new_incidents.append(item)

    incident_kinds = Counter(str((i or {}).get("kind") or "unknown") for i in new_incidents)

    usage_after = after.get("usage_recent") or {}

    return {
        "available": True,
        "rotation_state": {
            "mode_before": before_rotation.get("mode"),
            "mode_after": after_rotation.get("mode"),
            "active_key_slot_before": before_rotation.get("active_key_slot"),
            "active_key_slot_after": after_rotation.get("active_key_slot"),
            "active_worker_slot_before": before_rotation.get("active_worker_slot"),
            "active_worker_slot_after": after_rotation.get("active_worker_slot"),
            "workers": worker_delta,
            "keys": key_delta,
        },
        "incidents": {
            "total_before": before_total,
            "total_after": after_total,
            "total_delta": incidents_total_delta,
            "new_incidents_detected": len(new_incidents),
            "new_incidents_by_kind": dict(incident_kinds),
        },
        "usage_recent_after": usage_after,
    }


def print_kv(title: str, value: Any) -> None:
    print(f"{title}: {value}")


def print_counter(title: str, data: Dict[str, Any]) -> None:
    if not data:
        print(f"{title}: -")
        return
    items = ", ".join(f"{k}={v}" for k, v in sorted(data.items(), key=lambda x: str(x[0])))
    print(f"{title}: {items}")


def print_cli_summary(report: Dict[str, Any], report_path: Path) -> None:
    meta = report.get("meta") or {}
    summary = report.get("summary") or {}
    latency = report.get("latency_ms") or {}
    proxy = report.get("proxy_observability") or {}
    admin = (report.get("admin_telemetry") or {}).get("delta") or {}
    fail = report.get("failure_analysis") or {}

    print("\n===== Load Test Report =====")
    print_kv("Run ID", meta.get("run_id"))
    print_kv("Started", meta.get("started_at"))
    print_kv("Ended", meta.get("ended_at"))
    print_kv("Duration", short_duration(float(meta.get("duration_seconds") or 0.0)))

    print("\n--- Traffic ---")
    print_kv("Total Requests", summary.get("total_requests"))
    print_kv("Success", summary.get("success_count"))
    print_kv("Failure", summary.get("failure_count"))
    print_kv("Success Rate", f"{summary.get('success_rate', 0)}%")
    print_kv("Planned RPS", summary.get("planned_rps"))
    print_kv("Achieved RPS", summary.get("achieved_rps"))

    print("\n--- Latency (ms) ---")
    print_kv("min", latency.get("min"))
    print_kv("mean", latency.get("mean"))
    print_kv("p50", latency.get("p50"))
    print_kv("p90", latency.get("p90"))
    print_kv("p95", latency.get("p95"))
    print_kv("p99", latency.get("p99"))
    print_kv("max", latency.get("max"))

    print("\n--- Status / Errors ---")
    print_counter("Status Counts", summary.get("status_counts") or {})
    print_counter("Status Classes", summary.get("status_class_counts") or {})
    print_counter("Error Types", summary.get("error_type_counts") or {})

    if to_int(summary.get("failure_count"), 0) > 0:
        print("\n--- Failure Root Cause ---")
        print_counter("By Location", fail.get("by_location") or {})
        print_counter("By Cause", fail.get("by_cause") or {})
        print_counter("By HTTP Status", fail.get("by_status_code") or {})
        print_counter("By Gemini Error Status", fail.get("by_error_status") or {})
        print_counter("Matched Incident Kinds", fail.get("correlated_incident_kinds") or {})

    print("\n--- Proxy / Worker / Key ---")
    print_counter("Served Via", proxy.get("served_via_counts") or {})
    print_counter("Worker Slots", proxy.get("worker_slot_counts") or {})
    print_counter("Key Slots", proxy.get("key_slot_counts") or {})
    print_kv("Key Rotated True Count", proxy.get("key_rotated_true_count"))
    print_kv("Attempts > 1", proxy.get("attempts_gt1_count"))
    print_kv("Estimated Retry Attempts", proxy.get("estimated_retry_attempts"))
    print_kv("Observed Key Slot Switches", proxy.get("observed_key_slot_switches"))
    print_kv("Observed Worker Slot Switches", proxy.get("observed_worker_slot_switches"))

    if admin.get("available"):
        print("\n--- Admin Delta ---")
        inc = admin.get("incidents") or {}
        print_kv("Incidents Delta", inc.get("total_delta"))
        print_counter("New Incidents by Kind", inc.get("new_incidents_by_kind") or {})

        rotation = admin.get("rotation_state") or {}
        workers = (rotation.get("workers") or {}).get("slots") or []
        if workers:
            print("Worker Runtime Delta:")
            for item in workers:
                print(
                    f"  slot={item.get('slot')} success_delta={item.get('success_count_delta')} "
                    f"failure_delta={item.get('failure_count_delta')}"
                )

        keys = (rotation.get("keys") or {}).get("slots") or []
        if keys:
            print("Key Runtime Delta:")
            for item in keys:
                print(
                    f"  slot={item.get('slot')} success_delta={item.get('success_count_delta')} "
                    f"failure_delta={item.get('failure_count_delta')}"
                )

    print("\nReport JSON:", report_path)
    print("============================\n")


def load_config(path: Path) -> LoadTestConfig:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError("Config YAML must be a mapping/object")

    try:
        return LoadTestConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid load test config: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gemini Proxy load test runner with observability report")
    parser.add_argument(
        "--config",
        default="config/load_test.example.yml",
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and print resolved config JSON only",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)

    cfg = load_config(config_path)

    if args.dry_run:
        print(json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2))
        return

    runner = LoadRunner(cfg)
    asyncio.run(runner.run())


if __name__ == "__main__":
    main()
