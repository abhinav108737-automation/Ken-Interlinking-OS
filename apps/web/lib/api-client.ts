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

// Phase 2 content intelligence (ADR-0010 section 7). Read-only.
export type IntelligenceCoverage = Schemas["IntelligenceCoverage"];
export type TaxonomyView = Schemas["TaxonomyView"];
export type TaxonomyNodeView = Schemas["TaxonomyNodeView"];
export type PageTypeView = Schemas["PageTypeView"];
export type PageIntelligenceView = Schemas["PageIntelligenceView"];
export type PageAnalysis = Schemas["PageAnalysis"];
export type PageEmbeddingInfo = Schemas["PageEmbeddingInfo"];
export type PageProtectionView = Schemas["PageProtectionView"];
export type LinkEligibility = Schemas["LinkEligibility"];
export type IneligibilityReason = Schemas["IneligibilityReason"];
export type Intent = Schemas["Intent"];
export type ProtectionSource = Schemas["ProtectionSource"];
export type EntityType = Schemas["EntityType"];
export type TopicScore = Schemas["TopicScore"];
export type GeographyMention = Schemas["GeographyMention"];
export type EntityMention = Schemas["EntityMention"];
export type SalientBlock = Schemas["SalientBlock"];
export type SimilarPage = Schemas["SimilarPage"];
export type PageRelationshipView = Schemas["PageRelationshipView"];
export type RelationshipType = Schemas["RelationshipType"];
export type JobKind = Schemas["JobKind"];
export type JobRunSummary = Schemas["JobRunSummary"];
export type JobRunDetail = Schemas["JobRunDetail"];

export type SimilarPageList = Schemas["ListResponse_SimilarPage_"];
export type RelationshipList = Schemas["ListResponse_PageRelationshipView_"];
export type JobRunList = Schemas["ListResponse_JobRunSummary_"];

export type ListTaxonomyNodePagesQuery = NonNullable<
  operations["listTaxonomyNodePages"]["parameters"]["query"]
>;
export type ListPageRelationshipsQuery = NonNullable<
  operations["listPageRelationships"]["parameters"]["query"]
>;
export type ListSimilarPagesQuery = NonNullable<operations["listSimilarPages"]["parameters"]["query"]>;
export type SemanticSearchQuery = operations["semanticSearch"]["parameters"]["query"];
export type ListJobRunsQuery = NonNullable<operations["listJobRuns"]["parameters"]["query"]>;

// Phase 3 recommendation engine (ADR-0011 section 8). Read-only.
export type LinkPlan = Schemas["LinkPlan"];
export type LinkPlanDetail = Schemas["LinkPlanDetail"];
export type LinkPlanStatus = Schemas["LinkPlanStatus"];
/** How the anchor text was chosen (distinct from the Phase 1 `AnchorSource` of existing links). */
export type PlanAnchorSource = LinkPlan["anchor_source"];
export type ScoreBreakdown = Schemas["ScoreBreakdown"];
export type ScoreComponent = Schemas["ScoreComponent"];
export type PlacementLocator = Schemas["PlacementLocator"];
export type PlacementPreview = Schemas["PlacementPreview"];
export type PlanTransitionView = Schemas["PlanTransitionView"];
export type ReasonCode = Schemas["ReasonCode"];
export type RetrievalChannel = Schemas["RetrievalChannel"];
export type CandidateRejection = Schemas["CandidateRejection"];
export type CandidateView = Schemas["CandidateView"];
export type OpportunityKind = Schemas["OpportunityKind"];
export type OpportunityView = Schemas["OpportunityView"];
export type RecommendationSummary = Schemas["RecommendationSummary"];

export type LinkPlanList = Schemas["ListResponse_LinkPlan_"];
export type CandidateList = Schemas["ListResponse_CandidateView_"];
export type OpportunityList = Schemas["ListResponse_OpportunityView_"];

export type ListLinkPlansQuery = NonNullable<operations["listLinkPlans"]["parameters"]["query"]>;
export type ListLinkCandidatesQuery = NonNullable<operations["listLinkCandidates"]["parameters"]["query"]>;
export type ListOpportunitiesQuery = NonNullable<operations["listOpportunities"]["parameters"]["query"]>;

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
 * - `api_error`        an error status the contract DOES declare for this path (404, 422,
 *                      or 503 `model_unavailable` for semantic search) with a contract
 *                      `ErrorResponse` body; `code` is always set.
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

/**
 * True when a detail screen should answer "no such resource": a declared 404,
 * an id the API rejects as malformed (422), or one the client refused to send.
 */
export function isMissingResourceError(error: ApiError): boolean {
  return (
    isNotFoundError(error) ||
    error.kind === "invalid_argument" ||
    (error.kind === "api_error" && error.status === 422)
  );
}

/** True for semantic search's declared 503: the embedding model cannot be reached. */
export function isModelUnavailableError(error: ApiError): boolean {
  return error.kind === "api_error" && error.status === 503 && error.code === "model_unavailable";
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

  // --- Phase 2 content intelligence: read-only -------------------------------

  /** `GET /intelligence/coverage` — analysis/embedding/eligibility coverage. Contract: 200 only. */
  getIntelligenceCoverage(options?: RequestOptions): Promise<ApiResult<IntelligenceCoverage>>;
  /** `GET /intelligence/taxonomy` — the active taxonomy policy with page counts. Contract: 200 only. */
  getTaxonomy(options?: RequestOptions): Promise<ApiResult<TaxonomyView>>;
  /** `GET /intelligence/taxonomy/{node_id}/pages` — pages listing a market or geography. Contract: 200, 404, 422. */
  listTaxonomyNodePages(
    nodeId: string,
    query?: ListTaxonomyNodePagesQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<SimilarPageList>>;
  /** `GET /intelligence/pages/{page_id}` — analysis, embedding, protections, eligibility. Contract: 200, 404, 422. */
  getPageIntelligence(pageId: string, options?: RequestOptions): Promise<ApiResult<PageIntelligenceView>>;
  /** `GET /intelligence/pages/{page_id}/similar` — nearest pages by stored embedding. Contract: 200, 404, 422. */
  listSimilarPages(
    pageId: string,
    query?: ListSimilarPagesQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<SimilarPageList>>;
  /** `GET /intelligence/pages/{page_id}/relationships` — typed edges, strongest first. Contract: 200, 404, 422. */
  listPageRelationships(
    pageId: string,
    query?: ListPageRelationshipsQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<RelationshipList>>;
  /**
   * `GET /intelligence/search` — semantic search. Contract: 200, 422 and 503.
   * A 503 is an `api_error` with code `model_unavailable` (see
   * `isModelUnavailableError`): the embedding model could not be reached.
   */
  semanticSearch(query: SemanticSearchQuery, options?: RequestOptions): Promise<ApiResult<SimilarPageList>>;

  // --- Generic job runs (ADR-0010 section 6): read-only ----------------------

  /** `GET /jobs/runs` — batch job runs, newest first. Contract: 200, 422. */
  listJobRuns(query?: ListJobRunsQuery, options?: RequestOptions): Promise<ApiResult<JobRunList>>;
  /** `GET /jobs/runs/{run_id}` — one job run with its configuration snapshot. Contract: 200, 404, 422. */
  getJobRun(runId: string, options?: RequestOptions): Promise<ApiResult<JobRunDetail>>;

  // --- Phase 3 recommendations (ADR-0011 section 8): read-only ---------------

  /** `GET /recommendations` — link plans, highest score first. Contract: 200, 422. */
  listLinkPlans(query?: ListLinkPlansQuery, options?: RequestOptions): Promise<ApiResult<LinkPlanList>>;
  /** `GET /recommendations/{plan_id}` — one plan with block text and status history. Contract: 200, 404, 422. */
  getLinkPlan(planId: string, options?: RequestOptions): Promise<ApiResult<LinkPlanDetail>>;
  /** `GET /recommendations/candidates` — retrieved candidates, kept or rejected. Contract: 200, 422. */
  listLinkCandidates(
    query?: ListLinkCandidatesQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<CandidateList>>;
  /** `GET /recommendations/opportunities` — orphan, under-linked and capacity pages. Contract: 200, 422. */
  listOpportunities(
    query?: ListOpportunitiesQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<OpportunityList>>;
  /** `GET /recommendations/summary` — dashboard counts. Contract: 200 only. */
  getRecommendationSummary(options?: RequestOptions): Promise<ApiResult<RecommendationSummary>>;
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

// --- Intelligence guards -----------------------------------------------------
// Same policy as above: every field the intelligence screens read is checked,
// enum membership is left to lib/intelligence.ts presentations.

function everyItem<T>(value: unknown, isItem: (item: unknown) => item is T): value is T[] {
  return Array.isArray(value) && value.every(isItem);
}

export function isIntelligenceCoverage(value: unknown): value is IntelligenceCoverage {
  return (
    isObject(value) &&
    [
      "total_pages",
      "analyzed_fresh",
      "analyzed_stale",
      "never_analyzed",
      "embedded_fresh",
      "embedded_stale_or_missing",
      "protected",
      "eligible_sources",
      "eligible_targets",
      "relationships",
    ].every((key) => typeof value[key] === "number") &&
    typeof value["analysis_version"] === "string" &&
    typeof value["embedding_model"] === "string" &&
    isNumberRecord(value["by_page_type"]) &&
    isNumberRecord(value["by_industry"])
  );
}

export function isTaxonomyNodeView(value: unknown): value is TaxonomyNodeView {
  return (
    isObject(value) &&
    typeof value["id"] === "string" &&
    typeof value["label"] === "string" &&
    typeof value["kind"] === "string" &&
    isNullableString(value["parent"]) &&
    typeof value["depth"] === "number" &&
    typeof value["page_count"] === "number"
  );
}

export function isPageTypeView(value: unknown): value is PageTypeView {
  return (
    isObject(value) &&
    typeof value["id"] === "string" &&
    typeof value["label"] === "string" &&
    typeof value["intent"] === "string" &&
    typeof value["commercial_importance"] === "number" &&
    typeof value["linkable_as_source"] === "boolean" &&
    typeof value["linkable_as_target"] === "boolean" &&
    typeof value["page_count"] === "number"
  );
}

export function isTaxonomyView(value: unknown): value is TaxonomyView {
  return (
    isObject(value) &&
    typeof value["version"] === "number" &&
    typeof value["status"] === "string" &&
    typeof value["source"] === "string" &&
    typeof value["analysis_version"] === "string" &&
    everyItem(value["markets"], isTaxonomyNodeView) &&
    everyItem(value["geographies"], isTaxonomyNodeView) &&
    everyItem(value["page_types"], isPageTypeView)
  );
}

export function isSimilarPage(value: unknown): value is SimilarPage {
  return (
    isObject(value) &&
    typeof value["page_id"] === "string" &&
    typeof value["canonical_url"] === "string" &&
    isNullableString(value["title"]) &&
    typeof value["score"] === "number" &&
    isNullableString(value["page_type"]) &&
    isNullableString(value["market_id"])
  );
}

export function isPageRelationshipView(value: unknown): value is PageRelationshipView {
  return (
    isObject(value) &&
    typeof value["source_page_id"] === "string" &&
    typeof value["source_page_version"] === "number" &&
    typeof value["target_page_id"] === "string" &&
    typeof value["target_page_version"] === "number" &&
    typeof value["target_canonical_url"] === "string" &&
    isNullableString(value["target_title"]) &&
    typeof value["relationship_type"] === "string" &&
    typeof value["weight"] === "number" &&
    isObject(value["evidence"]) &&
    typeof value["analysis_version"] === "string" &&
    typeof value["computed_at"] === "string"
  );
}

function isTopicScore(value: unknown): value is TopicScore {
  return (
    isObject(value) &&
    typeof value["node_id"] === "string" &&
    typeof value["label"] === "string" &&
    typeof value["score"] === "number" &&
    typeof value["depth"] === "number"
  );
}

function isGeographyMention(value: unknown): value is GeographyMention {
  return (
    isObject(value) &&
    typeof value["geo_id"] === "string" &&
    typeof value["label"] === "string" &&
    typeof value["kind"] === "string" &&
    typeof value["score"] === "number"
  );
}

function isEntityMention(value: unknown): value is EntityMention {
  return (
    isObject(value) &&
    typeof value["entity_id"] === "string" &&
    typeof value["label"] === "string" &&
    typeof value["type"] === "string" &&
    typeof value["mentions"] === "number"
  );
}

function isSalientBlock(value: unknown): value is SalientBlock {
  return (
    isObject(value) &&
    typeof value["block_id"] === "string" &&
    typeof value["score"] === "number" &&
    typeof value["text"] === "string"
  );
}

export function isPageAnalysis(value: unknown): value is PageAnalysis {
  return (
    isObject(value) &&
    typeof value["page_id"] === "string" &&
    typeof value["page_version"] === "number" &&
    typeof value["analysis_version"] === "string" &&
    typeof value["extractor"] === "string" &&
    typeof value["page_type"] === "string" &&
    typeof value["intent"] === "string" &&
    typeof value["commercial_importance"] === "number" &&
    isNullableString(value["industry_id"]) &&
    isNullableString(value["market_id"]) &&
    everyItem(value["topics"], isTopicScore) &&
    everyItem(value["geographies"], isGeographyMention) &&
    everyItem(value["entities"], isEntityMention) &&
    isStringArray(value["keywords"]) &&
    typeof value["summary"] === "string" &&
    everyItem(value["salient_blocks"], isSalientBlock)
  );
}

export function isPageEmbeddingInfo(value: unknown): value is PageEmbeddingInfo {
  return (
    isObject(value) &&
    typeof value["page_id"] === "string" &&
    typeof value["page_version"] === "number" &&
    typeof value["model"] === "string" &&
    typeof value["provider"] === "string" &&
    typeof value["dimensions"] === "number" &&
    typeof value["recipe_version"] === "number" &&
    typeof value["recipe_fingerprint"] === "string" &&
    typeof value["input_hash"] === "string" &&
    typeof value["created_at"] === "string"
  );
}

export function isPageProtectionView(value: unknown): value is PageProtectionView {
  return (
    isObject(value) &&
    typeof value["id"] === "number" &&
    typeof value["page_id"] === "string" &&
    typeof value["source"] === "string" &&
    isNullableString(value["rule_id"]) &&
    typeof value["reason"] === "string" &&
    typeof value["active"] === "boolean" &&
    typeof value["created_by"] === "string" &&
    typeof value["created_at"] === "string"
  );
}

export function isLinkEligibility(value: unknown): value is LinkEligibility {
  return (
    isObject(value) &&
    typeof value["as_source"] === "boolean" &&
    typeof value["as_target"] === "boolean" &&
    isStringArray(value["source_reasons"]) &&
    isStringArray(value["target_reasons"])
  );
}

export function isPageIntelligenceView(value: unknown): value is PageIntelligenceView {
  return (
    isObject(value) &&
    typeof value["page_id"] === "string" &&
    typeof value["canonical_url"] === "string" &&
    isNullableString(value["title"]) &&
    typeof value["current_version"] === "number" &&
    (value["analysis"] === null || isPageAnalysis(value["analysis"])) &&
    typeof value["analysis_fresh"] === "boolean" &&
    (value["embedding"] === null || isPageEmbeddingInfo(value["embedding"])) &&
    typeof value["embedding_fresh"] === "boolean" &&
    everyItem(value["protections"], isPageProtectionView) &&
    isLinkEligibility(value["eligibility"]) &&
    isNumberRecord(value["relationship_counts"])
  );
}

function hasJobRunSummaryFields(value: JsonObject): boolean {
  return (
    typeof value["id"] === "string" &&
    typeof value["kind"] === "string" &&
    typeof value["status"] === "string" &&
    typeof value["triggered_by"] === "string" &&
    typeof value["started_at"] === "string" &&
    isNullableString(value["completed_at"]) &&
    isNumberRecord(value["counters"])
  );
}

export function isJobRunSummary(value: unknown): value is JobRunSummary {
  return isObject(value) && hasJobRunSummaryFields(value);
}

export function isJobRunDetail(value: unknown): value is JobRunDetail {
  return (
    isObject(value) &&
    hasJobRunSummaryFields(value) &&
    isObject(value["config_snapshot"]) &&
    isNullableString(value["failure_reason"])
  );
}

export const isSimilarPageList = (value: unknown): value is SimilarPageList =>
  isListResponse(value, isSimilarPage);
export const isRelationshipList = (value: unknown): value is RelationshipList =>
  isListResponse(value, isPageRelationshipView);
export const isJobRunList = (value: unknown): value is JobRunList => isListResponse(value, isJobRunSummary);

// --- Recommendation guards (Phase 3) -----------------------------------------
// Same policy: every field the recommendation screens read is checked,
// including the nested score breakdown, locator and preview; enum membership
// is left to lib/recommendations.ts presentations.

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function isScoreComponent(value: unknown): value is ScoreComponent {
  return (
    isObject(value) &&
    typeof value["name"] === "string" &&
    isNumber(value["value"]) &&
    isNumber(value["weight"]) &&
    isNumber(value["contribution"])
  );
}

export function isScoreBreakdown(value: unknown): value is ScoreBreakdown {
  return (
    isObject(value) &&
    everyItem(value["components"], isScoreComponent) &&
    isObject(value["factors"]) &&
    Object.values(value["factors"]).every(isNumber) &&
    typeof value["policy_name"] === "string" &&
    isNumber(value["policy_version"]) &&
    isNumber(value["relevance"]) &&
    (value["semantic_floor"] === null || isNumber(value["semantic_floor"])) &&
    isNumber(value["total"])
  );
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}

export function isPlacementLocator(value: unknown): value is PlacementLocator {
  return (
    isObject(value) &&
    typeof value["block_id"] === "string" &&
    typeof value["block_kind"] === "string" &&
    typeof value["context_hash"] === "string" &&
    isNonNegativeInteger(value["occurrence_index"]) &&
    isNonNegativeInteger(value["start"]) &&
    isNonNegativeInteger(value["end"]) &&
    (value["end"] as number) > (value["start"] as number)
  );
}

export function isPlacementPreview(value: unknown): value is PlacementPreview {
  return (
    isObject(value) &&
    typeof value["before"] === "string" &&
    typeof value["anchor"] === "string" &&
    typeof value["after"] === "string"
  );
}

export function isPlanTransitionView(value: unknown): value is PlanTransitionView {
  return (
    isObject(value) &&
    typeof value["id"] === "number" &&
    typeof value["plan_id"] === "string" &&
    isNullableString(value["from_status"]) &&
    typeof value["to_status"] === "string" &&
    typeof value["actor"] === "string" &&
    isNullableString(value["note"]) &&
    typeof value["occurred_at"] === "string"
  );
}

function hasLinkPlanFields(value: JsonObject): boolean {
  return (
    typeof value["id"] === "string" &&
    typeof value["status"] === "string" &&
    typeof value["source_page_id"] === "string" &&
    typeof value["source_page_version"] === "number" &&
    typeof value["source_url"] === "string" &&
    typeof value["target_page_id"] === "string" &&
    typeof value["target_page_version"] === "number" &&
    typeof value["target_url"] === "string" &&
    isNullableString(value["target_title"]) &&
    typeof value["anchor_text"] === "string" &&
    typeof value["anchor_source"] === "string" &&
    isNullableString(value["relationship_type"]) &&
    isStringArray(value["channels"]) &&
    isStringArray(value["reason_codes"]) &&
    typeof value["rationale"] === "string" &&
    isNumber(value["confidence"]) &&
    isNullableString(value["calibration_version"]) &&
    isScoreBreakdown(value["score"]) &&
    isPlacementLocator(value["locator"]) &&
    isPlacementPreview(value["preview"]) &&
    typeof value["engine_version"] === "string" &&
    typeof value["policy_version"] === "number" &&
    typeof value["ranking_policy_version"] === "number" &&
    typeof value["analysis_version"] === "string" &&
    isNullableString(value["embedding_model"]) &&
    isNullableString(value["reasoning_model"]) &&
    isNullableString(value["run_id"]) &&
    isNullableString(value["supersedes"]) &&
    typeof value["created_at"] === "string" &&
    typeof value["updated_at"] === "string"
  );
}

export function isLinkPlan(value: unknown): value is LinkPlan {
  return isObject(value) && hasLinkPlanFields(value);
}

export function isLinkPlanDetail(value: unknown): value is LinkPlanDetail {
  return (
    isObject(value) &&
    hasLinkPlanFields(value) &&
    typeof value["source_block_text"] === "string" &&
    isNullableString(value["source_title"]) &&
    everyItem(value["transitions"], isPlanTransitionView)
  );
}

export function isCandidateView(value: unknown): value is CandidateView {
  return (
    isObject(value) &&
    typeof value["run_id"] === "string" &&
    typeof value["source_page_id"] === "string" &&
    typeof value["target_page_id"] === "string" &&
    typeof value["target_url"] === "string" &&
    isStringArray(value["channels"]) &&
    isNumberRecord(value["channel_scores"]) &&
    (value["total_score"] === null || isNumber(value["total_score"])) &&
    isNullableString(value["rejection"]) &&
    isNullableString(value["plan_id"])
  );
}

export function isOpportunityView(value: unknown): value is OpportunityView {
  return (
    isObject(value) &&
    typeof value["page_id"] === "string" &&
    typeof value["canonical_url"] === "string" &&
    isNullableString(value["title"]) &&
    typeof value["kind"] === "string" &&
    isNumber(value["score"]) &&
    isObject(value["evidence"]) &&
    typeof value["run_id"] === "string" &&
    typeof value["computed_at"] === "string"
  );
}

export function isRecommendationSummary(value: unknown): value is RecommendationSummary {
  return (
    isObject(value) &&
    isNumberRecord(value["plans_by_status"]) &&
    isNumberRecord(value["opportunities_by_kind"]) &&
    typeof value["sources_with_plans"] === "number" &&
    typeof value["targets_with_plans"] === "number" &&
    isNullableString(value["last_run_id"]) &&
    typeof value["policy_version"] === "number" &&
    typeof value["ranking_policy_version"] === "number"
  );
}

export const isLinkPlanList = (value: unknown): value is LinkPlanList => isListResponse(value, isLinkPlan);
export const isCandidateList = (value: unknown): value is CandidateList => isListResponse(value, isCandidateView);
export const isOpportunityList = (value: unknown): value is OpportunityList =>
  isListResponse(value, isOpportunityView);

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

/**
 * Taxonomy node ids, exactly as the contract's `node_id` pattern. The leading
 * alphanumeric means `.` / `..` can never be a whole segment, and `/`, `%`, `?`
 * and `#` are not allowed at all.
 */
const NODE_ID = /^[a-z0-9][a-z0-9_.-]{0,99}$/;

export function isSafeNodeId(value: unknown): value is string {
  return typeof value === "string" && NODE_ID.test(value);
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
              : response.status === 503
                ? `The API reported that a service needed for ${path} is unavailable (HTTP 503).`
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

  /** Read-only GET: 200 is data; `declaredErrors` (default 404/422) are contract `ErrorResponse`s. */
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

    getIntelligenceCoverage: (callOptions = {}) =>
      inventory(
        "/intelligence/coverage",
        "/intelligence/coverage",
        isIntelligenceCoverage,
        undefined,
        callOptions,
        [],
      ),

    getTaxonomy: (callOptions = {}) =>
      inventory("/intelligence/taxonomy", "/intelligence/taxonomy", isTaxonomyView, undefined, callOptions, []),

    listTaxonomyNodePages: (nodeId, query, callOptions = {}) => {
      const path = "/intelligence/taxonomy/{node_id}/pages";
      if (!isSafeNodeId(nodeId)) return invalidArgument(path, "taxonomy node id");
      return inventory(path, `/intelligence/taxonomy/${id(nodeId)}/pages`, isSimilarPageList, query, callOptions);
    },

    getPageIntelligence: (pageId, callOptions = {}) => {
      const path = "/intelligence/pages/{page_id}";
      if (!isSafePathId(pageId)) return invalidArgument(path, "page id");
      return inventory(path, `/intelligence/pages/${id(pageId)}`, isPageIntelligenceView, undefined, callOptions);
    },

    listSimilarPages: (pageId, query, callOptions = {}) => {
      const path = "/intelligence/pages/{page_id}/similar";
      if (!isSafePathId(pageId)) return invalidArgument(path, "page id");
      return inventory(path, `/intelligence/pages/${id(pageId)}/similar`, isSimilarPageList, query, callOptions);
    },

    listPageRelationships: (pageId, query, callOptions = {}) => {
      const path = "/intelligence/pages/{page_id}/relationships";
      if (!isSafePathId(pageId)) return invalidArgument(path, "page id");
      return inventory(
        path,
        `/intelligence/pages/${id(pageId)}/relationships`,
        isRelationshipList,
        query,
        callOptions,
      );
    },

    semanticSearch: (query, callOptions = {}) =>
      inventory("/intelligence/search", "/intelligence/search", isSimilarPageList, query, callOptions, [422, 503]),

    listJobRuns: (query, callOptions = {}) =>
      inventory("/jobs/runs", "/jobs/runs", isJobRunList, query, callOptions, [422]),

    getJobRun: (runId, callOptions = {}) => {
      const path = "/jobs/runs/{run_id}";
      if (!isSafePathId(runId)) return invalidArgument(path, "run id");
      return inventory(path, `/jobs/runs/${id(runId)}`, isJobRunDetail, undefined, callOptions);
    },

    listLinkPlans: (query, callOptions = {}) =>
      inventory("/recommendations", "/recommendations", isLinkPlanList, query, callOptions, [422]),

    getLinkPlan: (planId, callOptions = {}) => {
      const path = "/recommendations/{plan_id}";
      if (!isSafePathId(planId)) return invalidArgument(path, "recommendation id");
      return inventory(path, `/recommendations/${id(planId)}`, isLinkPlanDetail, undefined, callOptions);
    },

    listLinkCandidates: (query, callOptions = {}) =>
      inventory(
        "/recommendations/candidates",
        "/recommendations/candidates",
        isCandidateList,
        query,
        callOptions,
        [422],
      ),

    listOpportunities: (query, callOptions = {}) =>
      inventory(
        "/recommendations/opportunities",
        "/recommendations/opportunities",
        isOpportunityList,
        query,
        callOptions,
        [422],
      ),

    getRecommendationSummary: (callOptions = {}) =>
      inventory(
        "/recommendations/summary",
        "/recommendations/summary",
        isRecommendationSummary,
        undefined,
        callOptions,
        [],
      ),
  };
}
