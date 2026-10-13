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
import type { components, operations, paths } from "./types.gen";

type Schemas = components["schemas"];

export type HealthResponse = Schemas["HealthResponse"];
export type ReadinessResponse = Schemas["ReadinessResponse"];
export type MetaResponse = Schemas["MetaResponse"];
export type DependencyCheck = Schemas["DependencyCheck"];
export type ServiceIdentity = Schemas["ServiceIdentity"];
export type ErrorCode = Schemas["ErrorCode"];
export type ErrorResponse = Schemas["ErrorResponse"];

// Phase 1 content inventory (ADR-0009). Read-only.
export type PageInfo = Schemas["PageInfo"];
export type PageSummary = Schemas["PageSummary"];
export type PageDetail = Schemas["PageDetail"];
export type PageVersionSummary = Schemas["PageVersionSummary"];
export type PageVersionDetail = Schemas["PageVersionDetail"];
export type Heading = Schemas["Heading"];
export type ExistingLinkView = Schemas["ExistingLinkView"];
export type IngestionRunSummary = Schemas["IngestionRunSummary"];
export type IngestionRunDetail = Schemas["IngestionRunDetail"];
export type IngestionIssueView = Schemas["IngestionIssueView"];
export type RunCounters = Schemas["RunCounters"];
export type PublicationState = Schemas["PublicationState"];
export type CanonicalStatus = Schemas["CanonicalStatus"];
export type LinkType = Schemas["LinkType"];
export type LinkResolution = Schemas["LinkResolution"];
export type AnchorSource = Schemas["AnchorSource"];
export type RunStatus = Schemas["RunStatus"];
export type SourceType = Schemas["SourceType"];
export type IssueSeverity = Schemas["IssueSeverity"];
export type IssueCode = Schemas["IssueCode"];

export type PageList = Schemas["ListResponse_PageSummary_"];
export type PageVersionList = Schemas["ListResponse_PageVersionSummary_"];
export type LinkList = Schemas["ListResponse_ExistingLinkView_"];
export type RunList = Schemas["ListResponse_IngestionRunSummary_"];
export type IssueList = Schemas["ListResponse_IngestionIssueView_"];

/** Query parameters of each list operation, exactly as the contract declares them. */
export type ListInventoryPagesQuery = NonNullable<operations["listInventoryPages"]["parameters"]["query"]>;
export type ListInventoryPageVersionsQuery = NonNullable<
  operations["listInventoryPageVersions"]["parameters"]["query"]
>;
export type ListInventoryPageLinksQuery = NonNullable<
  operations["listInventoryPageLinks"]["parameters"]["query"]
>;
export type ListInventoryLinksQuery = NonNullable<operations["listInventoryLinks"]["parameters"]["query"]>;
export type ListIngestionRunsQuery = NonNullable<operations["listIngestionRuns"]["parameters"]["query"]>;
export type ListIngestionRunIssuesQuery = NonNullable<
  operations["listIngestionRunIssues"]["parameters"]["query"]
>;

export type ApiPath = keyof paths;

export const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
export const DEFAULT_TIMEOUT_MS = 5000;

/**
 * Why a call produced no usable data.
 *
 * - `config`           the base URL is not a valid http(s) URL.
 * - `invalid_argument` a path argument (id, version) was malformed; nothing was sent.
 * - `network`          the request never got an HTTP response (refused, DNS, reset).
 * - `timeout`          no response within the timeout.
 * - `api_error`        an error status the contract DOES declare for this path (404, 422)
 *                      with a contract `ErrorResponse` body; `code` is always set.
 * - `http_status`      an HTTP status the contract does not define for this path.
 * - `invalid_body`     the body was not JSON or did not match the contract shape.
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
  /** `ErrorResponse.request_id`, for correlating with server logs, when supplied. */
  readonly requestId?: string;
}

/** True for a contract-declared 404 (`page_not_found` / `not_found`). */
export function isNotFoundError(error: ApiError): boolean {
  return error.kind === "api_error" && error.status === 404;
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

  // --- Phase 1 content inventory: read-only, contract 200 | 404 | 422 -------

  /** `GET /inventory/pages` — page inventory, newest change first. Contract: 200, 422. */
  listInventoryPages(
    query?: ListInventoryPagesQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<PageList>>;
  /** `GET /inventory/pages/{page_id}` — one page with its current version. Contract: 200, 404, 422. */
  getInventoryPage(pageId: string, options?: RequestOptions): Promise<ApiResult<PageDetail>>;
  /** `GET /inventory/pages/{page_id}/versions` — version history. Contract: 200, 404, 422. */
  listInventoryPageVersions(
    pageId: string,
    query?: ListInventoryPageVersionsQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<PageVersionList>>;
  /** `GET /inventory/pages/{page_id}/versions/{version}` — one stored version. Contract: 200, 404, 422. */
  getInventoryPageVersion(
    pageId: string,
    version: number,
    options?: RequestOptions,
  ): Promise<ApiResult<PageVersionDetail>>;
  /** `GET /inventory/pages/{page_id}/links` — body links of one version. Contract: 200, 404, 422. */
  listInventoryPageLinks(
    pageId: string,
    query?: ListInventoryPageLinksQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<LinkList>>;
  /** `GET /inventory/links` — body links across current versions. Contract: 200, 422. */
  listInventoryLinks(
    query?: ListInventoryLinksQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<LinkList>>;
  /** `GET /inventory/runs` — crawl and import runs, newest first. Contract: 200, 422. */
  listIngestionRuns(
    query?: ListIngestionRunsQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<RunList>>;
  /** `GET /inventory/runs/{run_id}` — one run. Contract: 200, 404, 422. */
  getIngestionRun(runId: string, options?: RequestOptions): Promise<ApiResult<IngestionRunDetail>>;
  /** `GET /inventory/runs/{run_id}/issues` — per-URL issues of one run. Contract: 200, 404, 422. */
  listIngestionRunIssues(
    runId: string,
    query?: ListIngestionRunIssuesQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<IssueList>>;
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

// --- Inventory guards ------------------------------------------------------
// They check every field the inventory screens read, including the ones whose
// absence would crash rendering (arrays, nested objects). Enum membership is
// left to lib/inventory.ts presentations, as above.

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === "string";
}

function isNullableNumber(value: unknown): boolean {
  return value === null || typeof value === "number";
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isNumberRecord(value: unknown): boolean {
  return isObject(value) && Object.values(value).every((item) => typeof item === "number");
}

function isStringRecord(value: unknown): boolean {
  return isObject(value) && Object.values(value).every((item) => typeof item === "string");
}

export function isPageInfo(value: unknown): value is PageInfo {
  return (
    isObject(value) &&
    typeof value["total"] === "number" &&
    typeof value["limit"] === "number" &&
    typeof value["offset"] === "number" &&
    typeof value["has_more"] === "boolean"
  );
}

/** Guard for the shared `ListResponse_*` envelope: `{ items: T[], page: PageInfo }`. */
export function isListResponse<T>(
  value: unknown,
  isItem: (item: unknown) => item is T,
): value is { items: T[]; page: PageInfo } {
  return (
    isObject(value) &&
    Array.isArray(value["items"]) &&
    value["items"].every(isItem) &&
    isPageInfo(value["page"])
  );
}

function hasPageSummaryFields(value: JsonObject): boolean {
  return (
    typeof value["id"] === "string" &&
    typeof value["canonical_url"] === "string" &&
    typeof value["content_hash"] === "string" &&
    typeof value["current_version"] === "number" &&
    typeof value["publication_state"] === "string" &&
    typeof value["word_count"] === "number" &&
    typeof value["internal_link_count"] === "number" &&
    typeof value["external_link_count"] === "number" &&
    typeof value["first_seen_at"] === "string" &&
    typeof value["last_seen_at"] === "string" &&
    typeof value["last_changed_at"] === "string" &&
    isNullableString(value["gone_at"]) &&
    isNullableString(value["title"]) &&
    isNullableString(value["language"]) &&
    isNullableString(value["page_type"]) &&
    isNullableString(value["external_id"])
  );
}

export function isPageSummary(value: unknown): value is PageSummary {
  return isObject(value) && hasPageSummaryFields(value);
}

function hasVersionSummaryFields(value: JsonObject): boolean {
  return (
    typeof value["page_id"] === "string" &&
    typeof value["version"] === "number" &&
    typeof value["content_hash"] === "string" &&
    typeof value["url"] === "string" &&
    typeof value["fetched_at"] === "string" &&
    typeof value["created_at"] === "string" &&
    typeof value["source_type"] === "string" &&
    typeof value["source_id"] === "string" &&
    typeof value["publication_state"] === "string" &&
    typeof value["word_count"] === "number" &&
    typeof value["internal_link_count"] === "number" &&
    typeof value["external_link_count"] === "number" &&
    isNullableNumber(value["http_status"]) &&
    isNullableString(value["title"]) &&
    isNullableString(value["run_id"])
  );
}

export function isPageVersionSummary(value: unknown): value is PageVersionSummary {
  return isObject(value) && hasVersionSummaryFields(value);
}

function isHeading(value: unknown): value is Heading {
  return (
    isObject(value) &&
    typeof value["level"] === "number" &&
    typeof value["text"] === "string" &&
    typeof value["block_id"] === "string"
  );
}

export function isPageVersionDetail(value: unknown): value is PageVersionDetail {
  return (
    isObject(value) &&
    hasVersionSummaryFields(value) &&
    typeof value["body_text"] === "string" &&
    typeof value["body_html"] === "string" &&
    typeof value["canonical_url"] === "string" &&
    typeof value["canonical_status"] === "string" &&
    typeof value["requested_url"] === "string" &&
    isNullableString(value["declared_canonical_url"]) &&
    isNullableString(value["language"]) &&
    isNullableString(value["meta_description"]) &&
    isNullableString(value["page_type"]) &&
    isNullableString(value["published_at"]) &&
    isNullableString(value["modified_at"]) &&
    Array.isArray(value["headings"]) &&
    value["headings"].every(isHeading) &&
    isStringRecord(value["metadata"]) &&
    isStringArray(value["robots_directives"]) &&
    isStringArray(value["redirect_chain"])
  );
}

export function isPageDetail(value: unknown): value is PageDetail {
  return isObject(value) && hasPageSummaryFields(value) && isPageVersionDetail(value["current"]);
}

export function isExistingLinkView(value: unknown): value is ExistingLinkView {
  return (
    isObject(value) &&
    typeof value["page_id"] === "string" &&
    typeof value["page_version"] === "number" &&
    typeof value["source_url"] === "string" &&
    typeof value["ordinal"] === "number" &&
    typeof value["href"] === "string" &&
    typeof value["anchor_text"] === "string" &&
    typeof value["anchor_source"] === "string" &&
    typeof value["link_type"] === "string" &&
    typeof value["resolution"] === "string" &&
    typeof value["block_id"] === "string" &&
    typeof value["block_kind"] === "string" &&
    typeof value["occurrence_index"] === "number" &&
    typeof value["context_text"] === "string" &&
    isNullableString(value["resolved_url"]) &&
    isNullableString(value["target_page_id"]) &&
    isStringArray(value["rel"])
  );
}

const RUN_COUNTER_KEYS: Readonly<Record<keyof RunCounters, true>> = {
  pages_discovered: true,
  pages_fetched: true,
  pages_created: true,
  pages_changed: true,
  pages_unchanged: true,
  pages_duplicate: true,
  pages_failed: true,
  pages_skipped: true,
  pages_gone: true,
  error_count: true,
  warning_count: true,
};

export function isRunCounters(value: unknown): value is RunCounters {
  return (
    isObject(value) &&
    (Object.keys(RUN_COUNTER_KEYS) as (keyof RunCounters)[]).every(
      (key) => typeof value[key] === "number",
    )
  );
}

function hasRunSummaryFields(value: JsonObject): boolean {
  return (
    typeof value["id"] === "string" &&
    typeof value["source_type"] === "string" &&
    typeof value["source_name"] === "string" &&
    typeof value["status"] === "string" &&
    typeof value["started_at"] === "string" &&
    isNullableString(value["completed_at"]) &&
    isRunCounters(value["counters"])
  );
}

export function isIngestionRunSummary(value: unknown): value is IngestionRunSummary {
  return isObject(value) && hasRunSummaryFields(value);
}

export function isIngestionRunDetail(value: unknown): value is IngestionRunDetail {
  return (
    isObject(value) &&
    hasRunSummaryFields(value) &&
    isObject(value["config_snapshot"]) &&
    isNumberRecord(value["issue_counts"]) &&
    isNullableString(value["failure_reason"])
  );
}

export function isIngestionIssueView(value: unknown): value is IngestionIssueView {
  return (
    isObject(value) &&
    typeof value["id"] === "number" &&
    typeof value["run_id"] === "string" &&
    typeof value["code"] === "string" &&
    typeof value["severity"] === "string" &&
    typeof value["message"] === "string" &&
    typeof value["occurred_at"] === "string" &&
    isNullableString(value["url"]) &&
    isNullableString(value["related_url"]) &&
    isNullableNumber(value["http_status"])
  );
}

export const isPageList = (value: unknown): value is PageList => isListResponse(value, isPageSummary);
export const isPageVersionList = (value: unknown): value is PageVersionList =>
  isListResponse(value, isPageVersionSummary);
export const isLinkList = (value: unknown): value is LinkList =>
  isListResponse(value, isExistingLinkView);
export const isRunList = (value: unknown): value is RunList =>
  isListResponse(value, isIngestionRunSummary);
export const isIssueList = (value: unknown): value is IssueList =>
  isListResponse(value, isIngestionIssueView);

/** The contract's only error body. `details` may be absent from older servers. */
export function isErrorResponse(value: unknown): value is ErrorResponse {
  return (
    isObject(value) &&
    typeof value["code"] === "string" &&
    typeof value["message"] === "string" &&
    (value["details"] === undefined || Array.isArray(value["details"])) &&
    isOptionalNullableString(value["request_id"])
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

/** Query values as the contract types them; `null`/`undefined` mean "not sent". */
type QueryValue = string | number | boolean | null | undefined;

/**
 * Encode query parameters with URLSearchParams (never string concatenation).
 * Omitted, null and empty-string values are not sent: the contract treats an
 * absent filter as "any", whereas `q=` would be rejected as too short.
 */
export function buildQuery(query: Readonly<Record<string, QueryValue>> | undefined): URLSearchParams {
  const params = new URLSearchParams();
  if (query === undefined) {
    return params;
  }
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    params.append(key, String(value));
  }
  return params;
}

/**
 * Path identifiers (page and run ids) are UUIDs in the contract. Anything that
 * is not plainly an identifier is refused before a request is built, so a
 * crafted value (`..`, `%2e%2e`, `a/b`) can never retarget the request path.
 * Accepted values are still percent-encoded.
 */
const PATH_ID = /^[0-9A-Za-z-]{1,64}$/;

export function isSafePathId(value: unknown): value is string {
  return typeof value === "string" && PATH_ID.test(value);
}

/** A concrete request: the contract path template (reported in errors) plus the real URL path. */
interface ResolvedRequest {
  readonly path: ApiPath;
  readonly urlPath: string;
  readonly query?: URLSearchParams;
}

interface ResponseSpec<T> {
  /** Statuses whose body is the success schema. */
  readonly ok: readonly number[];
  /** Error statuses the contract declares for this path; their body is an `ErrorResponse`. */
  readonly declaredErrors: readonly number[];
  readonly guard: (value: unknown) => value is T;
}

async function getJson<T>(
  base: URL,
  fetchImpl: FetchLike,
  request: ResolvedRequest,
  spec: ResponseSpec<T>,
  timeoutMs: number,
): Promise<ApiResult<T>> {
  const { path } = request;
  // Preserve any path prefix on the base URL (e.g. behind a reverse proxy).
  const url = new URL(
    request.urlPath.replace(/^\//, ""),
    base.href.endsWith("/") ? base : `${base.href}/`,
  );
  const search = request.query?.toString() ?? "";
  if (search !== "") {
    url.search = search;
  }

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

  if (spec.declaredErrors.includes(response.status)) {
    if (parsed && isErrorResponse(body)) {
      const requestId = typeof body.request_id === "string" ? body.request_id : undefined;
      return {
        ok: false,
        error: {
          kind: "api_error",
          path,
          status: response.status,
          code: body.code,
          message:
            response.status === 404
              ? `The API reported that the requested ${path} resource does not exist.`
              : `The API rejected the request to ${path} (HTTP ${response.status}).`,
          ...(requestId === undefined ? {} : { requestId }),
        },
      };
    }
    return {
      ok: false,
      error: {
        kind: "invalid_body",
        path,
        status: response.status,
        message: `The API error response for ${path} did not match the expected contract.`,
      },
    };
  }

  if (!spec.ok.includes(response.status)) {
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

  if (!parsed || !spec.guard(body)) {
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
    request: ResolvedRequest,
    spec: ResponseSpec<T>,
    callOptions: RequestOptions,
  ): Promise<ApiResult<T>> {
    if (base === null) {
      return Promise.resolve(
        configError<T>(request.path, "The API base URL is not a valid http(s) URL."),
      );
    }
    return getJson(base, fetchImpl, request, spec, callOptions.timeoutMs ?? defaultTimeoutMs);
  }

  function invalidArgument<T>(path: ApiPath, what: string): Promise<ApiResult<T>> {
    return Promise.resolve({
      ok: false,
      error: { kind: "invalid_argument", path, message: `The ${what} is not valid; no request was sent.` },
    });
  }

  /** Read-only inventory GET: 200 is data; 404/422 are contract `ErrorResponse`s. */
  function inventory<T>(
    path: ApiPath,
    urlPath: string,
    guard: (value: unknown) => value is T,
    query: Readonly<Record<string, QueryValue>> | undefined,
    callOptions: RequestOptions,
    declaredErrors: readonly number[] = [404, 422],
  ): Promise<ApiResult<T>> {
    return call({ path, urlPath, query: buildQuery(query) }, { ok: [200], declaredErrors, guard }, callOptions);
  }

  const id = encodeURIComponent;

  return {
    getHealth: (callOptions = {}) =>
      call({ path: "/health", urlPath: "/health" }, { ok: [200], declaredErrors: [], guard: isHealthResponse }, callOptions),
    getReady: (callOptions = {}) =>
      call(
        { path: "/ready", urlPath: "/ready" },
        { ok: [200, 503], declaredErrors: [], guard: isReadinessResponse },
        callOptions,
      ),
    getMeta: (callOptions = {}) =>
      call({ path: "/meta", urlPath: "/meta" }, { ok: [200], declaredErrors: [], guard: isMetaResponse }, callOptions),

    listInventoryPages: (query, callOptions = {}) =>
      inventory("/inventory/pages", "/inventory/pages", isPageList, query, callOptions, [422]),

    getInventoryPage: (pageId, callOptions = {}) => {
      const path = "/inventory/pages/{page_id}";
      if (!isSafePathId(pageId)) return invalidArgument(path, "page id");
      return inventory(path, `/inventory/pages/${id(pageId)}`, isPageDetail, undefined, callOptions);
    },

    listInventoryPageVersions: (pageId, query, callOptions = {}) => {
      const path = "/inventory/pages/{page_id}/versions";
      if (!isSafePathId(pageId)) return invalidArgument(path, "page id");
      return inventory(path, `/inventory/pages/${id(pageId)}/versions`, isPageVersionList, query, callOptions);
    },

    getInventoryPageVersion: (pageId, version, callOptions = {}) => {
      const path = "/inventory/pages/{page_id}/versions/{version}";
      if (!isSafePathId(pageId)) return invalidArgument(path, "page id");
      if (!Number.isSafeInteger(version) || version < 1) return invalidArgument(path, "version number");
      return inventory(
        path,
        `/inventory/pages/${id(pageId)}/versions/${id(String(version))}`,
        isPageVersionDetail,
        undefined,
        callOptions,
      );
    },

    listInventoryPageLinks: (pageId, query, callOptions = {}) => {
      const path = "/inventory/pages/{page_id}/links";
      if (!isSafePathId(pageId)) return invalidArgument(path, "page id");
      return inventory(path, `/inventory/pages/${id(pageId)}/links`, isLinkList, query, callOptions);
    },

    listInventoryLinks: (query, callOptions = {}) =>
      inventory("/inventory/links", "/inventory/links", isLinkList, query, callOptions, [422]),

    listIngestionRuns: (query, callOptions = {}) =>
      inventory("/inventory/runs", "/inventory/runs", isRunList, query, callOptions, [422]),

    getIngestionRun: (runId, callOptions = {}) => {
      const path = "/inventory/runs/{run_id}";
      if (!isSafePathId(runId)) return invalidArgument(path, "run id");
      return inventory(path, `/inventory/runs/${id(runId)}`, isIngestionRunDetail, undefined, callOptions);
    },

    listIngestionRunIssues: (runId, query, callOptions = {}) => {
      const path = "/inventory/runs/{run_id}/issues";
      if (!isSafePathId(runId)) return invalidArgument(path, "run id");
      return inventory(path, `/inventory/runs/${id(runId)}/issues`, isIssueList, query, callOptions);
    },
  };
}
