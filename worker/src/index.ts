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

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "POST") {
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
    if (parts.length !== 3 || parts[1] !== "models") {
      return new Response("Path must be /<prefix>/<apiVersion>/models/<model>:<method>", { status: 400 });
    }

    const apiVersion = parts[0];
    const modelAndMethod = parts[2];
    const payload = await request.text();

    const retryOn429 = toBool(env.RETRY_ON_429, true);
    const roundsLimit = Math.max(1, parseInt(env.MAX_RETRIES_PER_KEY || "2", 10));
    const cooloffMs = Math.max(0, parseInt(env.COOLOFF_MS || "0", 10));

    let roundsDone = 0;
    let triedInRound = 0;

    while (true) {
      const key = keys[inMemoryKeyIndex % keys.length];
      const target = buildGeminiUrl(env, apiVersion, modelAndMethod, url.search);

      const headers = new Headers(request.headers);
      headers.set("x-goog-api-key", key);
      headers.delete("host");
      headers.delete(authHeaderName(env));

      const upstreamResp = await fetch(target, {
        method: "POST",
        headers,
        body: payload,
      });

      if (!retryOn429) {
        return upstreamResp;
      }

      const bodyText = await upstreamResp.clone().text();
      if (!is429OrExhausted(upstreamResp.status, bodyText)) {
        return upstreamResp;
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
