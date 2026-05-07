export interface Env {
  GEMINI_API_KEYS: string;
  GEMINI_BASE_URL?: string;
  WORKER_ROUTE_PREFIX?: string;
  WORKER_AUTH_TOKEN?: string;
  WORKER_AUTH_HEADER_NAME?: string;
  RETRY_ON_429?: string;
  MAX_RETRIES_PER_KEY?: string;
  COOLOFF_MS?: string;
}

let inMemoryKeyIndex = 0;

function toBool(value: string | undefined, defaultValue: boolean): boolean {
  if (!value) return defaultValue;
  return ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
}

function getKeys(env: Env): string[] {
  return (env.GEMINI_API_KEYS || "")
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

function is429OrExhausted(status: number, bodyText: string): boolean {
  if (status === 429) return true;
  return bodyText.toUpperCase().includes("RESOURCE_EXHAUSTED");
}

function normalizePrefix(prefix?: string): string {
  let out = (prefix || "/gemini").trim();
  if (!out.startsWith("/")) out = `/${out}`;
  return out.replace(/\/$/, "");
}

function authHeaderName(env: Env): string {
  return (env.WORKER_AUTH_HEADER_NAME || "x-worker-auth").toLowerCase();
}

function buildGeminiUrl(env: Env, apiVersion: string, modelAndMethod: string, query: string): string {
  const base = (env.GEMINI_BASE_URL || "https://generativelanguage.googleapis.com").replace(/\/$/, "");
  return `${base}/${apiVersion}/models/${modelAndMethod}${query}`;
}

async function sleep(ms: number): Promise<void> {
  if (ms <= 0) return;
  await new Promise((resolve) => setTimeout(resolve, ms));
}

function withProxyMetadataHeaders(
  upstreamResp: Response,
  {
    keySlot,
    keyPoolSize,
    attempts,
  }: {
    keySlot: number;
    keyPoolSize: number;
    attempts: number;
  },
): Response {
  const out = new Response(upstreamResp.body, upstreamResp);
  out.headers.set("x-proxy-served-via", "cloudflare_worker");
  out.headers.set("x-proxy-key-slot", String(keySlot));
  out.headers.set("x-proxy-key-pool-size", String(keyPoolSize));
  out.headers.set("x-proxy-attempts", String(Math.max(1, attempts)));
  out.headers.set("x-proxy-key-rotated", attempts > 1 ? "true" : "false");
  return out;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "POST" && request.method !== "GET") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const keys = getKeys(env);
    if (!keys.length) {
      return new Response("Missing GEMINI_API_KEYS", { status: 500 });
    }

    const requiredToken = (env.WORKER_AUTH_TOKEN || "").trim();
    if (requiredToken) {
      const incoming = request.headers.get(authHeaderName(env)) || "";
      if (incoming !== requiredToken) {
        return new Response("Unauthorized", { status: 401 });
      }
    }

    const url = new URL(request.url);
    const prefix = normalizePrefix(env.WORKER_ROUTE_PREFIX);
    if (!url.pathname.startsWith(`${prefix}/`)) {
      return new Response(`Invalid path. Expected prefix: ${prefix}`, { status: 404 });
    }

    const suffix = url.pathname.slice(prefix.length + 1);
    const parts = suffix.split("/");
    if (!(parts.length === 2 && parts[1] === "models") && !(parts.length === 3 && parts[1] === "models")) {
      return new Response("Path must be /<prefix>/<apiVersion>/models or /<prefix>/<apiVersion>/models/<model>:<method>", { status: 400 });
    }

    const apiVersion = parts[0];
    const isModelsList = parts.length === 2;
    const modelAndMethod = isModelsList ? "" : parts[2];
    const payload = request.method === "POST" ? await request.text() : "";

    if (isModelsList && request.method !== "GET") {
      return new Response("Method Not Allowed for models list. Use GET.", { status: 405 });
    }

    if (!isModelsList && request.method !== "POST") {
      return new Response("Method Not Allowed for model method call. Use POST.", { status: 405 });
    }

    const retryOn429 = toBool(env.RETRY_ON_429, true);
    const roundsLimit = Math.max(1, parseInt(env.MAX_RETRIES_PER_KEY || "2", 10));
    const cooloffMs = Math.max(0, parseInt(env.COOLOFF_MS || "0", 10));

    let roundsDone = 0;
    let triedInRound = 0;
    let attempts = 0;

    while (true) {
      attempts += 1;
      const keySlot = (inMemoryKeyIndex % keys.length) + 1;
      const key = keys[inMemoryKeyIndex % keys.length];
      const target = isModelsList
        ? `${(env.GEMINI_BASE_URL || "https://generativelanguage.googleapis.com").replace(/\/$/, "")}/${apiVersion}/models${url.search}`
        : buildGeminiUrl(env, apiVersion, modelAndMethod, url.search);

      const headers = new Headers(request.headers);
      headers.set("x-goog-api-key", key);
      headers.delete("host");
      headers.delete(authHeaderName(env));

      const upstreamResp = await fetch(target, {
        method: request.method,
        headers,
        body: request.method === "POST" ? payload : undefined,
      });

      if (!retryOn429) {
        return withProxyMetadataHeaders(upstreamResp, {
          keySlot,
          keyPoolSize: keys.length,
          attempts,
        });
      }

      const bodyText = await upstreamResp.clone().text();
      if (!is429OrExhausted(upstreamResp.status, bodyText)) {
        return withProxyMetadataHeaders(upstreamResp, {
          keySlot,
          keyPoolSize: keys.length,
          attempts,
        });
      }

      triedInRound += 1;
      inMemoryKeyIndex = (inMemoryKeyIndex + 1) % keys.length;

      if (triedInRound >= keys.length) {
        roundsDone += 1;
        triedInRound = 0;

        if (roundsDone >= roundsLimit) {
          await sleep(cooloffMs);
          roundsDone = 0;
        }
      }
    }
  },
};
