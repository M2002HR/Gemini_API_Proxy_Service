from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError


class AppSection(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    request_timeout_sec: float = 120.0
    enable_docs: bool = True
    docs_url: str = "/docs"
    redoc_url: str = "/redoc"
    openapi_url: str = "/openapi.json"


class ProxySection(BaseModel):
    mode: Literal["cloudflare_worker", "gemini_direct"] = "cloudflare_worker"
    trust_env_proxy: bool = True
    retry_on_429: bool = True
    max_retries_per_key: int = 2
    cooloff_sec: float = 30.0
    min_interval_sec: float = 0.0


class GeminiSection(BaseModel):
    base_url: str = "https://generativelanguage.googleapis.com"
    api_version: str = "v1beta"
    default_model: str = "gemini-2.5-flash"
    api_keys: List[str] = Field(default_factory=list)
    default_request: Dict[str, Any] = Field(default_factory=dict)


class CloudflareSection(BaseModel):
    worker_base_urls: List[str] = Field(default_factory=list)
    worker_route_prefix: str = "/gemini"
    auth_token: str = ""
    auth_header_name: str = "x-worker-auth"
    access_client_id: str = ""
    access_client_secret: str = ""
    pass_trace_headers: bool = True


class AdminSection(BaseModel):
    enabled: bool = True
    require_auth: bool = False
    token: str = ""
    header_name: str = "x-admin-token"
    models_cache_ttl_sec: float = 300.0
    max_recent_requests: int = 2000
    max_incidents: int = 500


class Settings(BaseModel):
    app: AppSection = Field(default_factory=AppSection)
    proxy: ProxySection = Field(default_factory=ProxySection)
    gemini: GeminiSection = Field(default_factory=GeminiSection)
    cloudflare: CloudflareSection = Field(default_factory=CloudflareSection)
    admin: AdminSection = Field(default_factory=AdminSection)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _set_nested(data: Dict[str, Any], path: str, value: Any) -> None:
    keys = path.split(".")
    cursor = data
    for key in keys[:-1]:
        if key not in cursor or not isinstance(cursor[key], dict):
            cursor[key] = {}
        cursor = cursor[key]
    cursor[keys[-1]] = value


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a top-level mapping: {path}")
    return raw


def _apply_env_overrides(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    mapping: Dict[str, tuple[str, Any]] = {
        "APP_HOST": ("app.host", str),
        "APP_PORT": ("app.port", int),
        "APP_LOG_LEVEL": ("app.log_level", str),
        "APP_REQUEST_TIMEOUT_SEC": ("app.request_timeout_sec", float),
        "APP_ENABLE_DOCS": ("app.enable_docs", _parse_bool),
        "APP_DOCS_URL": ("app.docs_url", str),
        "APP_REDOC_URL": ("app.redoc_url", str),
        "APP_OPENAPI_URL": ("app.openapi_url", str),
        "PROXY_MODE": ("proxy.mode", str),
        "PROXY_TRUST_ENV_PROXY": ("proxy.trust_env_proxy", _parse_bool),
        "PROXY_RETRY_ON_429": ("proxy.retry_on_429", _parse_bool),
        "PROXY_MAX_RETRIES_PER_KEY": ("proxy.max_retries_per_key", int),
        "PROXY_COOLOFF_SEC": ("proxy.cooloff_sec", float),
        "PROXY_MIN_INTERVAL_SEC": ("proxy.min_interval_sec", float),
        "GEMINI_BASE_URL": ("gemini.base_url", str),
        "GEMINI_API_VERSION": ("gemini.api_version", str),
        "GEMINI_DEFAULT_MODEL": ("gemini.default_model", str),
        "GEMINI_API_KEYS": ("gemini.api_keys", _parse_csv),
        "CLOUDFLARE_WORKER_BASE_URLS": ("cloudflare.worker_base_urls", _parse_csv),
        "CLOUDFLARE_WORKER_ROUTE_PREFIX": ("cloudflare.worker_route_prefix", str),
        "CLOUDFLARE_AUTH_TOKEN": ("cloudflare.auth_token", str),
        "CLOUDFLARE_AUTH_HEADER_NAME": ("cloudflare.auth_header_name", str),
        "CLOUDFLARE_ACCESS_CLIENT_ID": ("cloudflare.access_client_id", str),
        "CLOUDFLARE_ACCESS_CLIENT_SECRET": ("cloudflare.access_client_secret", str),
        "CLOUDFLARE_PASS_TRACE_HEADERS": ("cloudflare.pass_trace_headers", _parse_bool),
        "ADMIN_ENABLED": ("admin.enabled", _parse_bool),
        "ADMIN_REQUIRE_AUTH": ("admin.require_auth", _parse_bool),
        "ADMIN_TOKEN": ("admin.token", str),
        "ADMIN_HEADER_NAME": ("admin.header_name", str),
        "ADMIN_MODELS_CACHE_TTL_SEC": ("admin.models_cache_ttl_sec", float),
        "ADMIN_MAX_RECENT_REQUESTS": ("admin.max_recent_requests", int),
        "ADMIN_MAX_INCIDENTS": ("admin.max_incidents", int),
    }

    merged = dict(config_dict)

    for env_name, (path, caster) in mapping.items():
        raw = os.getenv(env_name)
        if raw is None or raw == "":
            continue
        _set_nested(merged, path, caster(raw))

    default_request_raw = os.getenv("GEMINI_DEFAULT_REQUEST_JSON")
    if default_request_raw:
        parsed = json.loads(default_request_raw)
        if not isinstance(parsed, dict):
            raise ValueError("GEMINI_DEFAULT_REQUEST_JSON must be a JSON object")
        _set_nested(merged, "gemini.default_request", parsed)

    return merged


def load_settings(config_file: str | None = None) -> Settings:
    load_dotenv(override=False)

    default_data = Settings().model_dump()
    config_path = Path(config_file or os.getenv("APP_CONFIG_FILE", "config/config.yml"))
    file_data = _load_yaml(config_path)
    merged = _deep_merge(default_data, file_data)
    merged = _apply_env_overrides(merged)

    try:
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        raise ValueError(f"Invalid configuration: {exc}") from exc

    if settings.proxy.mode == "cloudflare_worker" and not settings.cloudflare.worker_base_urls:
        raise ValueError(
            "cloudflare.worker_base_urls is empty. Set it in config.yml or CLOUDFLARE_WORKER_BASE_URLS env."
        )

    if settings.proxy.mode == "cloudflare_worker":
        normalized_urls: List[str] = []
        for worker_url in settings.cloudflare.worker_base_urls:
            if "your-worker-subdomain.workers.dev" in worker_url:
                raise ValueError(
                    "cloudflare.worker_base_urls contains placeholder URL. "
                    "Replace it with your real deployed Worker URL."
                )
            if not worker_url.startswith(("http://", "https://")):
                worker_url = f"https://{worker_url}"
            normalized_urls.append(worker_url)
        settings.cloudflare.worker_base_urls = normalized_urls

    if settings.proxy.mode == "gemini_direct" and not settings.gemini.api_keys:
        raise ValueError("gemini.api_keys is empty for gemini_direct mode.")

    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
