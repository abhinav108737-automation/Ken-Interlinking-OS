/**
 * Server-side entry point for the Ken Interlink OS control API.
 *
 * SERVER-ONLY. The base URL comes from `process.env.KEN_API_BASE_URL`, which
 * has no `NEXT_PUBLIC_` prefix and is therefore never inlined into a browser
 * bundle. Import the request functions here only from server components or
 * route handlers.
 *
 * This file is the ONE place that reads the environment. The HTTP, parsing and
 * error-mapping core is environment-agnostic and lives in `lib/api-client.ts`
 * (reusable from a browser or a future desktop renderer via `createApiClient`).
 * Reusable code (components, lib/status, lib/platform) must import types from
 * `lib/api-client.ts`, not from here — ESLint enforces that.
 *
 * Functions never throw. They return an `ApiResult`, so a page can always
 * render either the data or a meaningful error state.
 */
import {
  type ApiClient,
  type ApiPath,
  type ApiResult,
  type HealthResponse,
  type MetaResponse,
  type ReadinessResponse,
  type RequestOptions,
  DEFAULT_API_BASE_URL,
  configError,
  createApiClient,
  parseBaseUrl,
} from "./api-client";

export type {
  ApiClient,
  ApiClientOptions,
  ApiError,
  ApiErrorKind,
  ApiPath,
  ApiResult,
  DependencyCheck,
  ErrorCode,
  FetchLike,
  HealthResponse,
  MetaResponse,
  ReadinessResponse,
  RequestOptions,
  ServiceIdentity,
} from "./api-client";
export {
  DEFAULT_API_BASE_URL,
  DEFAULT_TIMEOUT_MS,
  createApiClient,
  isHealthResponse,
  isMetaResponse,
  isReadinessResponse,
} from "./api-client";

/** The configured API base URL, or `null` when `KEN_API_BASE_URL` is not a valid http(s) URL. */
export function resolveBaseUrl(): URL | null {
  return parseBaseUrl(process.env["KEN_API_BASE_URL"]?.trim() || DEFAULT_API_BASE_URL);
}

// Resolved per call (not at import) so the environment is read at request time.
function withServerClient<T>(
  path: ApiPath,
  run: (client: ApiClient) => Promise<ApiResult<T>>,
): Promise<ApiResult<T>> {
  const baseUrl = resolveBaseUrl();
  if (baseUrl === null) {
    return Promise.resolve(configError<T>(path, "KEN_API_BASE_URL is not a valid http(s) URL."));
  }
  return run(createApiClient({ baseUrl }));
}

/** `GET /health` — liveness. Contract: 200 only. */
export function getHealth(options: RequestOptions = {}): Promise<ApiResult<HealthResponse>> {
  return withServerClient("/health", (client) => client.getHealth(options));
}

/**
 * `GET /ready` — readiness. Contract: 200 (ready / degraded) AND 503
 * (not_ready) carry the same `ReadinessResponse` body. A 503 is data, not an
 * error: it is the API reporting that a required dependency failed.
 */
export function getReady(options: RequestOptions = {}): Promise<ApiResult<ReadinessResponse>> {
  return withServerClient("/ready", (client) => client.getReady(options));
}

/** `GET /meta` — build and non-secret configuration facts. Contract: 200 only. */
export function getMeta(options: RequestOptions = {}): Promise<ApiResult<MetaResponse>> {
  return withServerClient("/meta", (client) => client.getMeta(options));
}
