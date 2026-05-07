from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .config import get_settings
from .services import ServiceContainer


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
    title="Gemini via Cloudflare Proxy",
    version="1.0.0",
    description="Config-driven Gemini proxy with Cloudflare Worker support and 429 failover.",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> Dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "proxy_mode": settings.proxy.mode,
        "default_model": settings.gemini.default_model,
    }


@app.post("/proxy/gemini")
async def proxy_gemini(request: Request, x_request_id: str | None = Header(default=None)) -> Response:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="JSON body must be an object")

    response = await request.app.state.services.gemini.proxy_call(
        payload,
        request_id=x_request_id,
    )

    return _to_response(response)


@app.post("/{api_version}/models/{model}:{method}")
async def gemini_compatible_route(
    api_version: str,
    model: str,
    method: str,
    request: Request,
    x_request_id: str | None = Header(default=None),
) -> Response:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="JSON body must be an object")

    response = await request.app.state.services.gemini.proxy_call(
        payload,
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

    content_type = headers["content-type"].lower()
    if "application/json" in content_type:
        try:
            data = resp.json()
            return JSONResponse(status_code=resp.status_code, content=data, headers=headers)
        except json.JSONDecodeError:
            pass

    return Response(status_code=resp.status_code, content=resp.content, headers=headers)
