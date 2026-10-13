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
  type IngestionRunDetail,
  type IssueList,
  type LinkList,
  type ListIngestionRunIssuesQuery,
  type ListIngestionRunsQuery,
  type ListInventoryLinksQuery,
  type ListInventoryPageLinksQuery,
  type ListInventoryPageVersionsQuery,
  type ListInventoryPagesQuery,
  type MetaResponse,
  type PageDetail,
  type PageList,
  type PageVersionDetail,
  type PageVersionList,
  type ReadinessResponse,
  type RequestOptions,
  type RunList,
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
  ExistingLinkView,
  FetchLike,
  HealthResponse,
  IngestionIssueView,
  IngestionRunDetail,
  IngestionRunSummary,
  IssueList,
  LinkList,
  MetaResponse,
  PageDetail,
  PageInfo,
  PageList,
  PageSummary,
  PageVersionDetail,
  PageVersionList,
  PageVersionSummary,
  ReadinessResponse,
  RequestOptions,
  RunList,
  ServiceIdentity,
} from "./api-client";
export {
  DEFAULT_API_BASE_URL,
  DEFAULT_TIMEOUT_MS,
  createApiClient,
  isHealthResponse,
  isMetaResponse,
  isNotFoundError,
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

// ---------------------------------------------------------------------------
// Phase 1 content inventory (ADR-0009 section 7). All read-only GETs.
// ---------------------------------------------------------------------------

/** `GET /inventory/pages` — page inventory. Contract: 200, 422. */
export function listInventoryPages(
  query: ListInventoryPagesQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<PageList>> {
  return withServerClient("/inventory/pages", (client) => client.listInventoryPages(query, options));
}

/** `GET /inventory/pages/{page_id}` — one page with its current version. Contract: 200, 404, 422. */
export function getInventoryPage(
  pageId: string,
  options: RequestOptions = {},
): Promise<ApiResult<PageDetail>> {
  return withServerClient("/inventory/pages/{page_id}", (client) =>
    client.getInventoryPage(pageId, options),
  );
}

/** `GET /inventory/pages/{page_id}/versions` — version history. Contract: 200, 404, 422. */
export function listInventoryPageVersions(
  pageId: string,
  query: ListInventoryPageVersionsQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<PageVersionList>> {
  return withServerClient("/inventory/pages/{page_id}/versions", (client) =>
    client.listInventoryPageVersions(pageId, query, options),
  );
}

/** `GET /inventory/pages/{page_id}/versions/{version}` — one stored version. Contract: 200, 404, 422. */
export function getInventoryPageVersion(
  pageId: string,
  version: number,
  options: RequestOptions = {},
): Promise<ApiResult<PageVersionDetail>> {
  return withServerClient("/inventory/pages/{page_id}/versions/{version}", (client) =>
    client.getInventoryPageVersion(pageId, version, options),
  );
}

/** `GET /inventory/pages/{page_id}/links` — body links of one version. Contract: 200, 404, 422. */
export function listInventoryPageLinks(
  pageId: string,
  query: ListInventoryPageLinksQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<LinkList>> {
  return withServerClient("/inventory/pages/{page_id}/links", (client) =>
    client.listInventoryPageLinks(pageId, query, options),
  );
}

/** `GET /inventory/links` — body links across current versions. Contract: 200, 422. */
export function listInventoryLinks(
  query: ListInventoryLinksQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<LinkList>> {
  return withServerClient("/inventory/links", (client) => client.listInventoryLinks(query, options));
}

/** `GET /inventory/runs` — crawl and import runs. Contract: 200, 422. */
export function listIngestionRuns(
  query: ListIngestionRunsQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<RunList>> {
  return withServerClient("/inventory/runs", (client) => client.listIngestionRuns(query, options));
}

/** `GET /inventory/runs/{run_id}` — one run. Contract: 200, 404, 422. */
export function getIngestionRun(
  runId: string,
  options: RequestOptions = {},
): Promise<ApiResult<IngestionRunDetail>> {
  return withServerClient("/inventory/runs/{run_id}", (client) => client.getIngestionRun(runId, options));
}

/** `GET /inventory/runs/{run_id}/issues` — issues of one run. Contract: 200, 404, 422. */
export function listIngestionRunIssues(
  runId: string,
  query: ListIngestionRunIssuesQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<IssueList>> {
  return withServerClient("/inventory/runs/{run_id}/issues", (client) =>
    client.listIngestionRunIssues(runId, query, options),
  );
}
