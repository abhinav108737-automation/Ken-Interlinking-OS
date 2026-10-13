/**
 * Environment-agnostic HTTP core for the Ken Interlink OS control API.
 *
 * This module is deliberately portable: it runs unchanged in a Next.js server
 * component, a browser, a test runner or a future Electron renderer. It must
 * never reference the Node `process` global, Node built-ins, the browser
 * `window`, Electron or a database client. ESLint enforces that (see
 * eslint.config.mjs) and tests/api-client.test.ts checks this source text.
 *
 * Callers supply the base URL (and optionally `fetch`) explicitly. The Next.js
 * server-side wiring that reads `KEN_API_BASE_URL` lives in `lib/api.ts`.
 *
 * Every response type comes from `types.gen.ts`, generated from the frozen
 * OpenAPI contract. Nothing here invents a shape.
 *
 * Calls never throw. They return an `ApiResult`, so a page can always render
 * either the data or a meaningful error state.
 */
import type { ApiErrorKind } from "./status";
import type { components, paths } from "./types.gen";

type Schemas = components["schemas"];

export type HealthResponse = Schemas["HealthResponse"];
export type ReadinessResponse = Schemas["ReadinessResponse"];
export type MetaResponse = Schemas["MetaResponse"];
export type DependencyCheck = Schemas["DependencyCheck"];
export type ServiceIdentity = Schemas["ServiceIdentity"];
export type ErrorCode = Schemas["ErrorCode"];

export type ApiPath = keyof paths;

export const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
export const DEFAULT_TIMEOUT_MS = 5000;

/**
 * Why a call produced no usable data.
 *
 * - `config`       the base URL is not a valid http(s) URL.
 * - `network`      the request never got an HTTP response (refused, DNS, reset).
 * - `timeout`      no response within the timeout.
 * - `http_status`  an HTTP status the contract does not define for this path.
 * - `invalid_body` the body was not JSON or did not match the contract shape.
 */
export type { ApiErrorKind };

export interface ApiError {
  readonly kind: ApiErrorKind;
  readonly path: ApiPath;
  /** Human-readable, safe to show to an operator. Never contains the base URL. */
  readonly message: string;
  /** HTTP status, when a response was received. */
  readonly status?: number;
  /** Stable error code, when the body was a contract `ErrorResponse`. */
  readonly code?: ErrorCode;
}

export type ApiResult<T> =
  | { readonly ok: true; readonly data: T; readonly status: number }
  | { readonly ok: false; readonly error: ApiError };

export interface RequestOptions {
  readonly timeoutMs?: number;
}

/** The subset of the WHATWG `fetch` signature the client uses. */
export type FetchLike = (input: URL, init: RequestInit) => Promise<Response>;

export interface ApiClientOptions {
  /** Absolute http(s) base URL of the control API. May carry a path prefix. */
  readonly baseUrl: string | URL;
  /** Defaults to the global `fetch`, looked up at call time. */
  readonly fetch?: FetchLike;
  /** Default per-request timeout; a per-call `timeoutMs` overrides it. */
  readonly timeoutMs?: number;
}

export interface ApiClient {
  /** `GET /health` — liveness. Contract: 200 only. */
  getHealth(options?: RequestOptions): Promise<ApiResult<HealthResponse>>;
  /**
   * `GET /ready` — readiness. Contract: 200 (ready / degraded) AND 503
   * (not_ready) carry the same `ReadinessResponse` body. A 503 is data, not an
   * error: it is the API reporting that a required dependency failed.
   */
  getReady(options?: RequestOptions): Promise<ApiResult<ReadinessResponse>>;
  /** `GET /meta` — build and non-secret configuration facts. Contract: 200 only. */
  getMeta(options?: RequestOptions): Promise<ApiResult<MetaResponse>>;
}

// ---------------------------------------------------------------------------
// Structural guards. Deliberately shallow: they verify what the UI relies on,
// and leave enum membership to lib/status.ts so an additive enum value from a
// newer backend renders as "Unknown" instead of failing the whole page.
// ---------------------------------------------------------------------------

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isOptionalObject(value: unknown): boolean {
  return value === undefined || isObject(value);
}

function isOptionalNullableString(value: unknown): boolean {
  return value === undefined || value === null || typeof value === "string";
}

function isServiceIdentity(value: unknown): value is ServiceIdentity {
  return (
    isObject(value) &&
    typeof value["service"] === "string" &&
    typeof value["version"] === "string" &&
    typeof value["environment"] === "string" &&
    isOptionalNullableString(value["git_sha"])
  );
}

function isDependencyCheck(value: unknown): value is DependencyCheck {
  return (
    isObject(value) &&
    typeof value["name"] === "string" &&
    typeof value["kind"] === "string" &&
    typeof value["required"] === "boolean" &&
    typeof value["status"] === "string" &&
    typeof value["latency_ms"] === "number" &&
    isOptionalObject(value["detail"]) &&
    isOptionalNullableString(value["error"])
  );
}

export function isReadinessResponse(value: unknown): value is ReadinessResponse {
  return (
    isObject(value) &&
    typeof value["status"] === "string" &&
    isServiceIdentity(value["identity"]) &&
    typeof value["timestamp"] === "string" &&
    typeof value["duration_ms"] === "number" &&
    Array.isArray(value["checks"]) &&
    value["checks"].every(isDependencyCheck)
  );
}

export function isHealthResponse(value: unknown): value is HealthResponse {
  return (
    isObject(value) &&
    typeof value["status"] === "string" &&
    isServiceIdentity(value["identity"]) &&
    typeof value["uptime_s"] === "number" &&
    typeof value["timestamp"] === "string"
  );
}

export function isMetaResponse(value: unknown): value is MetaResponse {
  return (
    isObject(value) &&
    isServiceIdentity(value["identity"]) &&
    typeof value["python_version"] === "string" &&
    typeof value["phase"] === "number" &&
    typeof value["auto_approve_enabled"] === "boolean" &&
    isOptionalObject(value["settings"])
  );
}

function errorCodeOf(body: unknown): ErrorCode | undefined {
  // ErrorResponse.code is a contract enum; only surface it when it is a string.
  return isObject(body) && typeof body["code"] === "string"
    ? (body["code"] as ErrorCode)
    : undefined;
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

/** Parse an absolute http(s) URL. Anything else (including blank) yields `null`. */
export function parseBaseUrl(raw: string | URL): URL | null {
  try {
    const url = new URL(raw);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return null;
    }
    return url;
  } catch {
    return null;
  }
}

/** A `config` failure for `path`. The message must never echo the base URL. */
export function configError<T>(path: ApiPath, message: string): ApiResult<T> {
  return { ok: false, error: { kind: "config", path, message } };
}

function isAbortTimeout(error: unknown): boolean {
  return (
    error instanceof Error && (error.name === "TimeoutError" || error.name === "AbortError")
  );
}

// Resolve the global lazily so a fetch installed (or stubbed) after import is used,
// and call it unbound so it is never invoked with a foreign `this`.
const globalFetch: FetchLike = (input, init) => globalThis.fetch(input, init);

async function getJson<T>(
  base: URL,
  fetchImpl: FetchLike,
  path: ApiPath,
  acceptedStatuses: readonly number[],
  guard: (value: unknown) => value is T,
  timeoutMs: number,
): Promise<ApiResult<T>> {
  // Preserve any path prefix on the base URL (e.g. behind a reverse proxy).
  const url = new URL(path.replace(/^\//, ""), base.href.endsWith("/") ? base : `${base.href}/`);

  let response: Response;
  try {
    response = await fetchImpl(url, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    return {
      ok: false,
      error: isAbortTimeout(error)
        ? { kind: "timeout", path, message: `The API did not respond to ${path} in time.` }
        : { kind: "network", path, message: `The API could not be reached for ${path}.` },
    };
  }

  let body: unknown;
  let parsed = true;
  try {
    body = await response.json();
  } catch (error) {
    if (isAbortTimeout(error)) {
      return {
        ok: false,
        error: {
          kind: "timeout",
          path,
          status: response.status,
          message: `The API response for ${path} did not complete in time.`,
        },
      };
    }
    parsed = false;
  }

  if (!acceptedStatuses.includes(response.status)) {
    const code = parsed ? errorCodeOf(body) : undefined;
    return {
      ok: false,
      error: {
        kind: "http_status",
        path,
        status: response.status,
        message: `The API answered ${path} with unexpected HTTP ${response.status}.`,
        ...(code === undefined ? {} : { code }),
      },
    };
  }

  if (!parsed || !guard(body)) {
    return {
      ok: false,
      error: {
        kind: "invalid_body",
        path,
        status: response.status,
        message: `The API response for ${path} did not match the expected contract.`,
      },
    };
  }

  return { ok: true, data: body, status: response.status };
}

/**
 * Build a client bound to an explicit base URL. An invalid base URL does not
 * throw: every call then returns a `config` error without touching the network.
 */
export function createApiClient(options: ApiClientOptions): ApiClient {
  const base = parseBaseUrl(options.baseUrl);
  const fetchImpl = options.fetch ?? globalFetch;
  const defaultTimeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  function call<T>(
    path: ApiPath,
    acceptedStatuses: readonly number[],
    guard: (value: unknown) => value is T,
    request: RequestOptions,
  ): Promise<ApiResult<T>> {
    if (base === null) {
      return Promise.resolve(configError<T>(path, "The API base URL is not a valid http(s) URL."));
    }
    return getJson(base, fetchImpl, path, acceptedStatuses, guard, request.timeoutMs ?? defaultTimeoutMs);
  }

  return {
    getHealth: (request = {}) => call("/health", [200], isHealthResponse, request),
    getReady: (request = {}) => call("/ready", [200, 503], isReadinessResponse, request),
    getMeta: (request = {}) => call("/meta", [200], isMetaResponse, request),
  };
}
