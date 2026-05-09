from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Path, Query, Request, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader

from .config import get_settings
from .schemas import (
    ConfigEffectiveResponse,
    HealthResponse,
    IncidentsResponse,
    KeysCheckResponse,
    KeysStatusResponse,
    LimitsInfoResponse,
    ModelsRefreshResponse,
    ModelsResponse,
    ModelsSummaryResponse,
    ProxyGeminiRequest,
    RotationStateResponse,
    UsageRecentResponse,
    WorkerConnectivityResponse,
)
from .services import ServiceContainer

_RUNTIME_SETTINGS = get_settings()
_DOCS_ENABLED = bool(_RUNTIME_SETTINGS.app.enable_docs)
_DOCS_URL = _RUNTIME_SETTINGS.app.docs_url if _DOCS_ENABLED else None
_REDOC_URL = _RUNTIME_SETTINGS.app.redoc_url if _DOCS_ENABLED else None
_OPENAPI_URL = _RUNTIME_SETTINGS.app.openapi_url if _DOCS_ENABLED else None

_ADMIN_HEADER_NAME = _RUNTIME_SETTINGS.admin.header_name
_ADMIN_API_KEY = APIKeyHeader(
    name=_ADMIN_HEADER_NAME,
    auto_error=False,
    scheme_name="AdminTokenAuth",
    description="Admin token for protected observability endpoints.",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    services = ServiceContainer(settings=settings)
    app.state.services = services
    app.state.settings = settings
    try:
        yield
    finally:
        await services.close()


app = FastAPI(
    title="Gemini Proxy Stack",
    version="1.1.0",
    description=(
        "Production-oriented Gemini proxy with Cloudflare Worker support, failover, "
        "admin observability, and comprehensive OpenAPI docs."
    ),
    docs_url=_DOCS_URL,
    redoc_url=_REDOC_URL,
    openapi_url=_OPENAPI_URL,
    swagger_ui_parameters={
        "tryItOutEnabled": True,
        "persistAuthorization": True,
        "defaultModelsExpandDepth": 1,
        "displayRequestDuration": True,
        "filter": True,
    },
    openapi_tags=[
        {"name": "health", "description": "Service liveness and runtime basics."},
        {"name": "proxy", "description": "Gemini proxy endpoints (simple + compatible routes)."},
        {"name": "admin-system", "description": "Effective config and runtime system state."},
        {"name": "admin-models", "description": "Gemini models inventory, filters, and cache refresh."},
        {"name": "admin-keys", "description": "Key/worker health checks and rotation state."},
        {"name": "admin-usage", "description": "Recent usage summaries and token aggregates."},
        {"name": "admin-limits", "description": "Quota/limit notes and relevant Gemini docs links."},
        {"name": "admin-connectivity", "description": "Worker/direct connectivity diagnostics."},
        {"name": "admin-incidents", "description": "Recent incidents/events from runtime telemetry."},
    ],
    lifespan=lifespan,
)


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "***"
    return value[:4] + "***" + value[-3:]


def _redacted_effective_config(settings) -> Dict[str, Any]:
    dump = settings.model_dump()

    keys = dump.get("gemini", {}).get("api_keys", [])
    dump["gemini"]["api_keys"] = [_mask_secret(k) for k in keys]
    dump["gemini"]["api_keys_count"] = len(keys)

    dump["cloudflare"]["auth_token"] = _mask_secret(dump["cloudflare"].get("auth_token", ""))
    dump["cloudflare"]["access_client_id"] = _mask_secret(dump["cloudflare"].get("access_client_id", ""))
    dump["cloudflare"]["access_client_secret"] = _mask_secret(dump["cloudflare"].get("access_client_secret", ""))

    dump["admin"]["token"] = _mask_secret(dump["admin"].get("token", ""))
    return dump


async def _require_admin(request: Request, admin_token: str | None = Security(_ADMIN_API_KEY)) -> None:
    settings = request.app.state.settings
    if not settings.admin.enabled:
        raise HTTPException(status_code=404, detail="Admin endpoints are disabled")

    if not settings.admin.require_auth:
        return

    expected = settings.admin.token
    provided = admin_token or ""
    if not expected or provided != expected:
        raise HTTPException(status_code=401, detail="Invalid admin token")


@app.get("/health", response_model=HealthResponse, tags=["health"], summary="Service health")
async def health(request: Request) -> Dict[str, Any]:
    settings = get_settings()
    started_at = request.app.state.services.gemini.started_at
    return {
        "status": "ok",
        "proxy_mode": settings.proxy.mode,
        "default_model": settings.gemini.default_model,
        "started_at": started_at,
    }


@app.get(
    "/admin/system/config-effective",
    response_model=ConfigEffectiveResponse,
    tags=["admin-system"],
    summary="Resolved effective configuration",
    dependencies=[Depends(_require_admin)],
)
async def admin_config_effective(request: Request) -> Dict[str, Any]:
    services = request.app.state.services
    settings = request.app.state.settings

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "precedence": "env > config.yml > defaults",
        "effective_config": _redacted_effective_config(settings),
        "runtime": {
            "service_started_at": services.gemini.started_at,
            "request_history_size": len(services.gemini.request_history),
            "incidents_size": len(services.gemini.incidents),
            "proxy_mode": settings.proxy.mode,
        },
    }


@app.get(
    "/admin/gemini/models",
    response_model=ModelsResponse,
    tags=["admin-models"],
    summary="List Gemini models",
    dependencies=[Depends(_require_admin)],
)
async def admin_models(
    request: Request,
    capability: Literal["all", "generate", "embed", "live"] = Query("all"),
    include_preview: bool = Query(True),
) -> Dict[str, Any]:
    return await request.app.state.services.gemini.get_models(
        capability=capability,
        include_preview=include_preview,
        force_refresh=False,
    )


@app.get(
    "/admin/gemini/models/summary",
    response_model=ModelsSummaryResponse,
    tags=["admin-models"],
    summary="Summarized Gemini models counts",
    dependencies=[Depends(_require_admin)],
)
async def admin_models_summary(request: Request) -> Dict[str, Any]:
    return await request.app.state.services.gemini.get_models_summary()


@app.post(
    "/admin/gemini/models/refresh",
    response_model=ModelsRefreshResponse,
    tags=["admin-models"],
    summary="Refresh models cache",
    dependencies=[Depends(_require_admin)],
)
async def admin_models_refresh(request: Request) -> Dict[str, Any]:
    return await request.app.state.services.gemini.refresh_models_cache()


@app.get(
    "/admin/keys/status",
    response_model=KeysStatusResponse,
    tags=["admin-keys"],
    summary="Current key/worker status snapshot",
    dependencies=[Depends(_require_admin)],
)
async def admin_keys_status(request: Request) -> Dict[str, Any]:
    return request.app.state.services.gemini.get_keys_status()


@app.post(
    "/admin/keys/check",
    response_model=KeysCheckResponse,
    tags=["admin-keys"],
    summary="Run active key/worker checks",
    dependencies=[Depends(_require_admin)],
)
async def admin_keys_check(request: Request) -> Dict[str, Any]:
    return await request.app.state.services.gemini.check_keys()


@app.get(
    "/admin/keys/rotation-state",
    response_model=RotationStateResponse,
    tags=["admin-keys"],
    summary="Round-robin rotation state",
    dependencies=[Depends(_require_admin)],
)
async def admin_rotation_state(request: Request) -> Dict[str, Any]:
    return request.app.state.services.gemini.get_rotation_state()


@app.get(
    "/admin/usage/recent",
    response_model=UsageRecentResponse,
    tags=["admin-usage"],
    summary="Recent usage aggregates",
    dependencies=[Depends(_require_admin)],
)
async def admin_usage_recent(request: Request, since_minutes: int = Query(60, ge=1, le=1440)) -> Dict[str, Any]:
    return request.app.state.services.gemini.get_recent_usage(since_minutes=since_minutes)


@app.get(
    "/admin/limits/info",
    response_model=LimitsInfoResponse,
    tags=["admin-limits"],
    summary="Limits/quota notes and links",
    dependencies=[Depends(_require_admin)],
)
async def admin_limits_info(request: Request) -> Dict[str, Any]:
    return request.app.state.services.gemini.get_limits_info()


@app.get(
    "/admin/worker/connectivity",
    response_model=WorkerConnectivityResponse,
    tags=["admin-connectivity"],
    summary="Connectivity diagnostics",
    dependencies=[Depends(_require_admin)],
)
async def admin_worker_connectivity(request: Request) -> Dict[str, Any]:
    return await request.app.state.services.gemini.check_worker_connectivity()


@app.get(
    "/admin/incidents",
    response_model=IncidentsResponse,
    tags=["admin-incidents"],
    summary="Recent incidents log",
    dependencies=[Depends(_require_admin)],
)
async def admin_incidents(request: Request, limit: int = Query(100, ge=1, le=2000)) -> Dict[str, Any]:
    return request.app.state.services.gemini.get_incidents(limit=limit)


@app.post(
    "/proxy/gemini",
    tags=["proxy"],
    summary="Simplified Gemini proxy endpoint",
)
async def proxy_gemini(
    request: Request,
    payload: ProxyGeminiRequest | None = Body(
        default=None,
        examples={
            "basic": {
                "summary": "Basic generateContent payload",
                "value": {
                    "model": "gemini-2.5-flash",
                    "contents": [{"role": "user", "parts": [{"text": "Say hello in Persian"}]}],
                },
            },
            "image_inline_data": {
                "summary": "Multimodal prompt (text + image)",
                "value": {
                    "model": "gemini-2.5-flash",
                    "contents": [
                        {
                            "role": "user",
                            "parts": [
                                {"text": "Describe this image in Persian"},
                                {"inlineData": {"mimeType": "image/jpeg", "data": "<BASE64_IMAGE_BYTES>"}},
                            ],
                        }
                    ],
                },
            },
        },
    ),
    x_request_id: str | None = Header(default=None),
) -> Response:
    if payload is None:
        body = {
            "model": request.app.state.settings.gemini.default_model,
            "contents": [{"role": "user", "parts": [{"text": "Say hello in Persian"}]}],
            "generationConfig": {"temperature": 0.3},
        }
    else:
        body = payload.model_dump(exclude_none=True)

    response = await request.app.state.services.gemini.proxy_call(
        body,
        request_id=x_request_id,
    )

    return _to_response(response)


@app.post(
    "/proxy/gemini/default",
    tags=["proxy"],
    summary="One-click default Gemini test",
)
async def proxy_gemini_default(
    request: Request,
    payload: ProxyGeminiRequest | None = Body(
        default=None,
        examples={
            "default": {
                "summary": "Optional override payload",
                "value": {
                    "contents": [{"role": "user", "parts": [{"text": "Say hello"}]}],
                },
            }
        },
    ),
    x_request_id: str | None = Header(default=None),
) -> Response:
    settings = request.app.state.settings
    body = {
        "model": settings.gemini.default_model,
        "api_version": settings.gemini.api_version,
        "method": "generateContent",
        "contents": [{"role": "user", "parts": [{"text": "Say hello in Persian"}]}],
        "generationConfig": {"temperature": 0.3},
    }
    if payload is not None:
        body.update(payload.model_dump(exclude_none=True))

    response = await request.app.state.services.gemini.proxy_call(
        body,
        request_id=x_request_id,
    )
    return _to_response(response)


@app.post(
    "/{api_version}/models/{model}:{method}",
    tags=["proxy"],
    summary="Gemini-compatible proxy route",
)
async def gemini_compatible_route(
    request: Request,
    api_version: str = Path(
        ..., description="Gemini API version", examples=["v1beta"],
    ),
    model: str = Path(
        ..., description="Gemini model name", examples=["gemini-2.5-flash"],
    ),
    method: str = Path(
        ..., description="Gemini method", examples=["generateContent"],
    ),
    payload: ProxyGeminiRequest | None = Body(
        default=None,
        examples={
            "compatible": {
                "summary": "Gemini-compatible payload",
                "value": {
                    "contents": [{"role": "user", "parts": [{"text": "Say hello"}]}],
                },
            },
            "compatible_image_inline_data": {
                "summary": "Gemini-compatible multimodal payload",
                "value": {
                    "contents": [
                        {
                            "role": "user",
                            "parts": [
                                {"text": "What do you see in this image?"},
                                {"inlineData": {"mimeType": "image/png", "data": "<BASE64_IMAGE_BYTES>"}},
                            ],
                        }
                    ],
                },
            }
        },
    ),
    x_request_id: str | None = Header(default=None),
) -> Response:
    if payload is None:
        body = {
            "contents": [{"role": "user", "parts": [{"text": "Say hello"}]}],
            "generationConfig": {"temperature": 0.3},
        }
    else:
        body = payload.model_dump(exclude_none=True)

    response = await request.app.state.services.gemini.proxy_call(
        body,
        path_model=model,
        path_api_version=api_version,
        path_method=method,
        request_id=x_request_id,
    )

    return _to_response(response)


def _to_response(resp) -> Response:
    headers = {
        "content-type": resp.headers.get("content-type", "application/json"),
    }

    def _parse_positive_int(raw: Any) -> int | None:
        try:
            parsed = int(str(raw))
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    served_via = str(resp.headers.get("x-proxy-served-via", "")).strip() or "unknown"
    proxy_metadata = {
        "served_via": served_via,
        "from_cloudflare_worker": served_via == "cloudflare_worker",
        "key_slot": _parse_positive_int(resp.headers.get("x-proxy-key-slot")),
        "key_pool_size": _parse_positive_int(resp.headers.get("x-proxy-key-pool-size")),
        "worker_slot": _parse_positive_int(resp.headers.get("x-proxy-worker-slot")),
        "worker_url": str(resp.headers.get("x-proxy-worker-url", "")).strip() or None,
        "attempts": _parse_positive_int(resp.headers.get("x-proxy-attempts")),
        "key_rotated": str(resp.headers.get("x-proxy-key-rotated", "")).strip().lower() == "true",
    }

    content_type = headers["content-type"].lower()
    if "application/json" in content_type:
        try:
            data = resp.json()
            if isinstance(data, dict):
                data["proxy_metadata"] = proxy_metadata
            return JSONResponse(status_code=resp.status_code, content=data, headers=headers)
        except json.JSONDecodeError:
            pass

    return Response(status_code=resp.status_code, content=resp.content, headers=headers)
