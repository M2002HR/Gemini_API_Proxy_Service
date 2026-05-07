#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

get_var() {
  local key="$1"
  local def="$2"
  local current="${!key-}"
  if [[ -n "$current" ]]; then
    echo "$current"
    return
  fi

  if [[ -f "$ENV_FILE" ]]; then
    local line
    line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
    if [[ -n "$line" ]]; then
      local raw="${line#*=}"
      # Trim optional surrounding single/double quotes.
      if [[ "$raw" =~ ^\".*\"$ ]]; then
        raw="${raw:1:${#raw}-2}"
      elif [[ "$raw" =~ ^\'.*\'$ ]]; then
        raw="${raw:1:${#raw}-2}"
      fi
      echo "$raw"
      return
    fi
  fi

  echo "$def"
}

APP_CONFIG_FILE_VAL="$(get_var APP_CONFIG_FILE config/config.yml)"
HOST="$(get_var APP_HOST 0.0.0.0)"
PORT="$(get_var APP_PORT 8000)"
LOG_LEVEL="$(get_var APP_LOG_LEVEL info)"
RELOAD="$(get_var APP_RELOAD false)"
PROXY_MODE_VAL="$(get_var PROXY_MODE cloudflare_worker)"

DOCS_ENABLED="$(get_var APP_ENABLE_DOCS true)"
DOCS_URL="$(get_var APP_DOCS_URL /docs)"
REDOC_URL="$(get_var APP_REDOC_URL /redoc)"
OPENAPI_URL="$(get_var APP_OPENAPI_URL /openapi.json)"

export APP_CONFIG_FILE="$APP_CONFIG_FILE_VAL"

cd "$ROOT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[start] Python virtualenv not found at .venv. Create it and install requirements first."
  exit 1
fi

echo "[start] root=$ROOT_DIR"
echo "[start] env_file=$ENV_FILE"
echo "[start] host=$HOST port=$PORT log_level=$LOG_LEVEL reload=$RELOAD"
echo "[start] proxy_mode=$PROXY_MODE_VAL"

if [[ "${DOCS_ENABLED,,}" == "true" || "${DOCS_ENABLED,,}" == "1" || "${DOCS_ENABLED,,}" == "yes" ]]; then
  echo "[start] swagger_ui=http://$HOST:$PORT$DOCS_URL"
  echo "[start] redoc=http://$HOST:$PORT$REDOC_URL"
  echo "[start] openapi=http://$HOST:$PORT$OPENAPI_URL"
else
  echo "[start] docs disabled (APP_ENABLE_DOCS=$DOCS_ENABLED)"
fi

UVICORN_ARGS=(api.app.main:app --host "$HOST" --port "$PORT" --log-level "${LOG_LEVEL,,}")
if [[ "${RELOAD,,}" == "true" || "${RELOAD,,}" == "1" || "${RELOAD,,}" == "yes" ]]; then
  UVICORN_ARGS+=(--reload)
fi

exec .venv/bin/uvicorn "${UVICORN_ARGS[@]}"
