from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    proxy_mode: Literal["cloudflare_worker", "gemini_direct"]
    default_model: str
    started_at: str


class ProxyGeminiRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: Optional[str] = Field(default=None, description="Gemini model name")
    api_version: Optional[str] = Field(default=None, description="Gemini API version")
    method: Optional[str] = Field(default=None, description="Gemini method (default: generateContent)")
    contents: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Gemini contents payload for generate/stream calls",
        examples=[[{"role": "user", "parts": [{"text": "Hello"}]}]],
    )


class ConfigEffectiveResponse(BaseModel):
    generated_at: str
    precedence: str
    effective_config: Dict[str, Any]
    runtime: Dict[str, Any]


class ModelInfo(BaseModel):
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    input_token_limit: Optional[int] = None
    output_token_limit: Optional[int] = None
    supported_generation_methods: List[str] = Field(default_factory=list)
    is_preview: bool


class ModelsResponse(BaseModel):
    capability: Literal["all", "generate", "embed", "live"]
    include_preview: bool
    from_cache: bool
    fetched_at: Optional[str] = None
    source_key_mask: Optional[str] = None
    total: int
    models: List[ModelInfo]


class ModelsSummaryResponse(BaseModel):
    from_cache: bool
    fetched_at: Optional[str] = None
    total_models: int
    generate_models: int
    embed_models: int
    live_models: int
    preview_models: int
    stable_models: int


class ModelsRefreshResponse(BaseModel):
    ok: bool
    fetched_at: Optional[str] = None
    total: int
    source_key_mask: Optional[str] = None


class KeyCheckItem(BaseModel):
    slot: int
    key_mask: str
    worker_url: Optional[str] = None
    models_access_ok: bool
    models_status: int
    models_latency_ms: float
    models_error: str
    generate_test_ok: bool
    generate_status: Optional[int] = None
    generate_latency_ms: Optional[float] = None
    generate_error: str


class KeysCheckResponse(BaseModel):
    ok: bool
    last_checked_at: Optional[str] = None
    items: List[KeyCheckItem]
    note: Optional[str] = None


class KeyStatusItem(BaseModel):
    slot: int
    key_mask: str
    worker_url: Optional[str] = None
    is_active: bool
    valid: Optional[bool] = None
    last_check_at: Optional[str] = None
    last_error: str
    models_access_ok: Optional[bool] = None
    generate_test_ok: Optional[bool] = None
    avg_latency_ms: Optional[float] = None
    success_rate_1h: Optional[float] = None
    runtime_success_count: int
    runtime_failure_count: int
    runtime_last_status: Optional[int] = None
    runtime_last_429_at: Optional[str] = None


class KeysStatusResponse(BaseModel):
    last_checked_at: Optional[str] = None
    total_keys: int
    items: List[KeyStatusItem]
    note: Optional[str] = None


class RotationStateResponse(BaseModel):
    mode: Literal["cloudflare_worker", "gemini_direct"]
    key_pool_size: int
    worker_pool_size: int
    active_key_slot: Optional[int] = None
    active_worker_slot: Optional[int] = None
    keys: List[Dict[str, Any]]
    workers: List[Dict[str, Any]]


class UsageRecentResponse(BaseModel):
    window_minutes: int
    requests_in_window: int
    status_classes: Dict[str, int]
    by_model: Dict[str, Any]
    by_key: Dict[str, Any]
    sample: List[Dict[str, Any]]


class LimitsInfoResponse(BaseModel):
    remaining_credit_per_key_supported: bool
    remaining_credit_note: str
    retry_policy: Dict[str, Any]
    recent_rate_limited_events: int
    docs: Dict[str, str]


class WorkerConnectivityResponse(BaseModel):
    proxy_mode: Literal["cloudflare_worker", "gemini_direct"]
    worker_endpoint: Optional[str] = None
    reachable: bool
    status_code: Optional[int] = None
    classification: str
    location: str
    detail: str
    latency_ms: Optional[float] = None


class IncidentsResponse(BaseModel):
    total: int
    returned: int
    items: List[Dict[str, Any]]
