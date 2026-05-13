# Gemini Proxy Stack (Cloudflare Worker + FastAPI)

A production-oriented proxy stack for Gemini API requests:

`Client -> FastAPI -> Cloudflare Worker -> Gemini API -> Cloudflare Worker -> FastAPI -> Client`

This repository is fully configuration-driven so you can move it to any environment and reconfigure via `config/config.yml` and/or `.env`.

## Features

- Cloudflare Worker proxy in front of Gemini API
- FastAPI gateway with Gemini-compatible endpoints
- Multimodal passthrough (`text + image`) for Gemini `generateContent`
- Round-robin failover on `429` / `RESOURCE_EXHAUSTED`
- Configurable retry rounds and cooldown
- Optional per-request rate spacing (`min_interval_sec`)
- Config precedence with environment overrides
- Optional Cloudflare Access service-token headers

## Project Layout

- `api/` FastAPI application
- `worker/` Cloudflare Worker
- `config/config.example.yml` example config
- `.env.example` example environment variables

## Requirements

- Python 3.10+
- Node.js 18+
- Cloudflare account for Worker deployment

## FastAPI Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config/config.example.yml config/config.yml
uvicorn api.app.main:app --host 0.0.0.0 --port 8000 --reload
```

## One-command Start (Server + Swagger)

Use the launcher script (loads `.env` and starts FastAPI):

```bash
./scripts/start.sh
```

Use a custom env file:

```bash
ENV_FILE=/path/to/.env ./scripts/start.sh
```

If docs are enabled, Swagger/ReDoc/OpenAPI URLs are printed at startup.

Swagger defaults:

- `Try it out` is enabled automatically.
- Request examples/defaults are prefilled for proxy endpoints.
- `POST /proxy/gemini/default` is available for immediate one-click real call testing.

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## Cloudflare Worker Setup

```bash
cd worker
npm install
npx wrangler login
npx wrangler secret put GEMINI_API_KEYS
npx wrangler secret put WORKER_AUTH_TOKEN
npx wrangler deploy
```

After deploy, set your worker URL in FastAPI config:

- `CLOUDFLARE_WORKER_BASE_URLS=https://<your-worker>.workers.dev`

## API Endpoints

### 1) Simplified proxy endpoint

`POST /proxy/gemini`

Example:

```bash
curl -X POST http://127.0.0.1:8000/proxy/gemini \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-flash",
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "Say hello in Persian"}]
      }
    ]
  }'
```

Image understanding example (`inlineData`):

```bash
IMG_B64=$(base64 -w 0 /path/to/image.jpg)
curl -X POST http://127.0.0.1:8000/proxy/gemini \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"gemini-2.5-flash\",
    \"contents\": [
      {
        \"role\": \"user\",
        \"parts\": [
          {\"text\": \"این تصویر را دقیق توضیح بده\"},
          {\"inlineData\": {\"mimeType\": \"image/jpeg\", \"data\": \"$IMG_B64\"}}
        ]
      }
    ]
  }"
```

Use a model that supports image input.

### 1.1) One-click default test endpoint

`POST /proxy/gemini/default`

Designed for Swagger UI: you can run it directly with `Execute` using default values.

### 2) Gemini-compatible route

`POST /{api_version}/models/{model}:{method}`

Example:

```bash
curl -X POST http://127.0.0.1:8000/v1beta/models/gemini-2.5-flash:generateContent \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "Say hello"}]
      }
    ]
  }'
```

Compatible multimodal example:

```bash
IMG_B64=$(base64 -w 0 /path/to/image.png)
curl -X POST http://127.0.0.1:8000/v1beta/models/gemini-2.5-flash:generateContent \
  -H "Content-Type: application/json" \
  -d "{
    \"contents\": [
      {
        \"role\": \"user\",
        \"parts\": [
          {\"text\": \"What objects are visible?\"},
          {\"inlineData\": {\"mimeType\": \"image/png\", \"data\": \"$IMG_B64\"}}
        ]
      }
    ]
  }"
```

## Admin / Observability Endpoints

All admin endpoints are under `/admin`.

Mode behavior:

- If `PROXY_MODE=cloudflare_worker`, Gemini-facing admin operations (models listing, key/health checks, connectivity checks) are routed through the Worker path.
- If `PROXY_MODE=gemini_direct`, they call Gemini directly using `GEMINI_API_KEYS`.

1. `GET /health`
2. `GET /admin/system/config-effective`
3. `GET /admin/gemini/models`
4. `GET /admin/gemini/models/summary`
5. `POST /admin/gemini/models/refresh`
6. `GET /admin/keys/status`
7. `POST /admin/keys/check`
8. `GET /admin/keys/rotation-state`
9. `GET /admin/usage/recent?since_minutes=60`
10. `GET /admin/limits/info`
11. `GET /admin/worker/connectivity`
12. `GET /admin/incidents?limit=100`

### Admin auth (optional)

By default, admin auth is disabled. To enforce auth:

```env
ADMIN_ENABLED=true
ADMIN_REQUIRE_AUTH=true
ADMIN_TOKEN=your-strong-token
ADMIN_HEADER_NAME=x-admin-token
```

Then pass the token in the configured header.

Swagger UI also supports testing admin endpoints by sending this header value.

## Configuration Model

Settings are loaded in this order:

1. Code defaults
2. `config/config.yml`
3. Environment variables (`.env` / process env)

Final precedence:

`env > config.yml > defaults`

### Important variables

- `APP_ENABLE_DOCS=true|false`
- `APP_DOCS_URL=/docs`
- `APP_REDOC_URL=/redoc`
- `APP_OPENAPI_URL=/openapi.json`
- `APP_RELOAD=true|false`
- `PROXY_MODE=cloudflare_worker|gemini_direct`
- `PROXY_TRUST_ENV_PROXY=true|false`
- `PROXY_RETRY_ON_429=true|false`
- `PROXY_MAX_RETRIES_PER_KEY=<int>`
- `PROXY_COOLOFF_SEC=<float>`
- `PROXY_MIN_INTERVAL_SEC=<float>`
- `GEMINI_DEFAULT_REQUEST_JSON=<json object>`
- `CLOUDFLARE_WORKER_BASE_URLS=<csv urls>`
- `CLOUDFLARE_AUTH_TOKEN=<string>`
- `CLOUDFLARE_ACCESS_CLIENT_ID=<string>` (optional)
- `CLOUDFLARE_ACCESS_CLIENT_SECRET=<string>` (optional)
- `ADMIN_ENABLED=true|false`
- `ADMIN_REQUIRE_AUTH=true|false`
- `ADMIN_TOKEN=<string>`
- `ADMIN_HEADER_NAME=<string>`
- `ADMIN_MODELS_CACHE_TTL_SEC=<float>`
- `ADMIN_MAX_RECENT_REQUESTS=<int>`
- `ADMIN_MAX_INCIDENTS=<int>`

## Failover Behavior

### Worker side

- Worker uses `GEMINI_API_KEYS` (comma-separated secret)
- On `429` or `RESOURCE_EXHAUSTED`, it rotates to next key
- After one full key cycle, this counts as one round
- Cooldown applies after `MAX_RETRIES_PER_KEY` rounds

### FastAPI side

- In `cloudflare_worker` mode, FastAPI rotates across worker base URLs (if multiple are configured)
- In `gemini_direct` mode, FastAPI rotates across `GEMINI_API_KEYS`

## Cloudflare Access Notes

If your Worker is protected by Cloudflare Access and you do not send Access credentials, requests may get redirected (`302`) to a login page.

Options:

1. Disable Access protection for that API route
2. Keep Access enabled and set:
   - `CLOUDFLARE_ACCESS_CLIENT_ID`
   - `CLOUDFLARE_ACCESS_CLIENT_SECRET`

These are sent as:

- `CF-Access-Client-Id`
- `CF-Access-Client-Secret`

## Security Recommendations

- Store Gemini keys only in Worker secrets (`wrangler secret put`)
- Keep `.env` and `config/config.yml` out of version control
- Use a strong `WORKER_AUTH_TOKEN`
- Restrict FastAPI ingress by network/IP where possible

## Troubleshooting

### 502: missing protocol in worker URL

If you see URL/protocol errors, ensure:

- `CLOUDFLARE_WORKER_BASE_URLS` includes `https://`

Example:

```env
CLOUDFLARE_WORKER_BASE_URLS=https://my-worker.my-subdomain.workers.dev
```

### 302 redirect to cloudflareaccess.com

Worker is behind Cloudflare Access and API is unauthenticated. Either disable Access for that route or provide Access service token env vars.

### 503 UNAVAILABLE from Gemini

This is upstream model capacity pressure. Retry, reduce request load, or switch model.

## Direct Mode (No Worker)

If you need to bypass Worker temporarily:

```env
PROXY_MODE=gemini_direct
GEMINI_API_KEYS=key1,key2,key3
```

FastAPI will call Gemini directly and apply local round-robin key failover.

## Test Suite

Run the full Python test suite:

```bash
PYTHONPATH=. .venv/bin/pytest -q
```

Run only multimodal/image-related tests:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_api.py -k multimodal_payload
PYTHONPATH=. .venv/bin/pytest -q tests/test_services.py -k inline_image_data
```

Run Worker type-check:

```bash
cd worker
npx tsc --noEmit
```

## Load Test + JSON Report

A configurable load-test runner is included:

- Script: `scripts/load_test.py`
- Config template: `config/load_test.example.yml`
- Report output: `reports/load-tests/<run-id>.json`

Run:

```bash
PYTHONPATH=. .venv/bin/python scripts/load_test.py --config config/load_test.example.yml
```

Validate config only (no traffic):

```bash
PYTHONPATH=. .venv/bin/python scripts/load_test.py --config config/load_test.example.yml --dry-run
```

### What you can configure

- Load profile: `total_requests`, `requests_per_second`, `concurrency`, `ramp_up_seconds`
- Run logging: `show_progress_logs`, `progress_interval_seconds` (live progress in CLI)
- Request behavior: `request_timeout_seconds`, connection limits, HTTP/2 toggle, `trust_env_proxy`
- Loopback safety: `disable_env_proxy_for_loopback` (for `localhost/127.0.0.1` auto-bypass env proxy)
- Endpoint mode: `proxy_gemini`, `proxy_gemini_default`, `gemini_compatible`
- Gemini routing: `model`, `api_version`, `method`
- Prompt shaping: template list, `prompt_min_chars`, `prompt_max_chars`, random suffix length
- Payload details: `generation_config`, `extra_body`, default-endpoint payload toggle
- Success criteria: `assertions.success_status_codes`
- Admin telemetry collection: rotation state, usage, incidents, worker connectivity, key checks
- Report sampling: number of slow/error/success samples and max stored error body length
- Failure forensics: `top_n_failure_signatures` for grouped root-cause signatures

### Report contents

The runner prints a human-readable CLI summary and writes a structured JSON report with:

- Throughput and success/failure rates
- Latency stats (`min/mean/p50/p90/p95/p99/max`)
- HTTP status/error distributions
- Proxy metadata analysis:
  - worker slot usage
  - key slot usage
  - observed slot switches
  - retry/rotation indicators (`attempts`, `key_rotated`)
- Worker/key performance buckets with per-slot success/failure and latency
- Token usage totals from `usageMetadata` when available
- Admin before/after snapshots plus delta:
  - rotation/runtime counters
  - incident growth and incident kinds
  - usage aggregate snapshot
- Failure analysis:
  - `by_location` (client / gateway / worker/upstream)
  - `by_cause` (rate-limit, auth/access, upstream 5xx, connect error, validation, ...)
  - HTTP/Gemini error status distributions
  - request-level incident correlation via `request_id`

`reports/load-tests/` is ignored in git to keep generated reports out of version control.

### Live execution flow in CLI

During execution you will see phase + progress logs, for example:

```text
[2026-05-13T00:01:10.123456+00:00] run_id=gemini-proxy-benchmark-... load test started
[2026-05-13T00:01:10.123900+00:00] collecting admin snapshot: before
[2026-05-13T00:01:10.500100+00:00] admin snapshot before collected
[2026-05-13T00:01:10.500300+00:00] sending traffic total=300 rps=8.0 concurrency=24 endpoint=proxy_gemini
[2026-05-13T00:01:12.501000+00:00] progress 16/300 (5.3%) success=14 failure=2 rps_now=7.99 rps_avg=7.91
[2026-05-13T00:01:14.501000+00:00] progress 32/300 (10.7%) success=28 failure=4 rps_now=8.00 rps_avg=7.95
...
[2026-05-13T00:01:48.111000+00:00] traffic phase completed
[2026-05-13T00:01:48.111200+00:00] collecting admin snapshot: after
[2026-05-13T00:01:48.620000+00:00] admin snapshot after collected
```
