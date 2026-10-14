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

// Phase 4 agent orchestration (ADR-0012 section 11). Read-only: runs start from the CLI.
export type AgentRunStatus = Schemas["AgentRunStatus"];
export type AgentOutcome = Schemas["AgentOutcome"];
export type AgentTrigger = Schemas["AgentTrigger"];
export type AgentNode = Schemas["AgentNode"];
export type AgentErrorCode = Schemas["AgentErrorCode"];
export type TriageDecision = Schemas["TriageDecision"];
export type StepStatus = Schemas["StepStatus"];
export type FallbackReason = Schemas["FallbackReason"];
export type CriticVerdict = Schemas["CriticVerdict"];
export type CriticReason = Schemas["CriticReason"];
export type ToolName = Schemas["ToolName"];
export type ToolPermission = Schemas["ToolPermission"];
export type ModelRole = Schemas["ModelRole"];
export type ToolCallRecord = Schemas["ToolCallRecord"];
export type AgentStepView = Schemas["AgentStepView"];
export type AgentPlanReview = Schemas["AgentPlanReview"];
export type AgentRunSummary = Schemas["AgentRunSummary"];
export type AgentRunDetail = Schemas["AgentRunDetail"];

export type AgentRunList = Schemas["ListResponse_AgentRunSummary_"];
/** `GET /agents/plans/{plan_id}/reviews` returns a bare array (empty when no agent reviewed the plan). */
export type AgentPlanReviewList = operations["listAgentPlanReviews"]["responses"][200]["content"]["application/json"];

export type ListAgentRunsQuery = NonNullable<operations["listAgentRuns"]["parameters"]["query"]>;

// Phase 5 reviewer control plane (ADR-0013). Auth, human review, audit, settings.
export type Role = Schemas["Role"];
export type Permission = Schemas["Permission"];
export type LoginRequest = Schemas["LoginRequest"];
export type SessionView = Schemas["SessionView"];
export type PrincipalView = Schemas["PrincipalView"];
export type ReviewAction = Schemas["ReviewAction"];
export type FeedbackReason = Schemas["FeedbackReason"];
export type BulkAction = Schemas["BulkAction"];
export type ApproveRequest = Schemas["ApproveRequest"];
export type RejectRequest = Schemas["RejectRequest"];
export type EditAnchorRequest = Schemas["EditAnchorRequest"];
export type ReplaceTargetRequest = Schemas["ReplaceTargetRequest"];
export type BulkReviewRequest = Schemas["BulkReviewRequest"];
export type ReviewResult = Schemas["ReviewResult"];
export type BulkItemResult = Schemas["BulkItemResult"];
export type BulkReviewResult = Schemas["BulkReviewResult"];
export type ReviewActionView = Schemas["ReviewActionView"];
export type TargetOption = Schemas["TargetOption"];
export type DashboardSummary = Schemas["DashboardSummary"];
export type EventName = Schemas["EventName"];
export type EventRecord = Schemas["EventRecord"];
export type PolicyDocument = Schemas["PolicyDocument"];
export type PolicyKind = PolicyDocument["kind"];
export type PoliciesView = Schemas["PoliciesView"];

export type ReviewActionList = Schemas["ListResponse_ReviewActionView_"];
export type EventList = Schemas["ListResponse_EventRecord_"];
/** `GET /recommendations/{plan_id}/review-actions` returns a bare array, oldest first. */
export type PlanReviewActionList = operations["listPlanReviewActions"]["responses"][200]["content"]["application/json"];
/** `GET /recommendations/{plan_id}/target-options` returns a bare array. */
export type TargetOptionList = operations["listPlanTargetOptions"]["responses"][200]["content"]["application/json"];

export type ListReviewActionsQuery = NonNullable<operations["listReviewActions"]["parameters"]["query"]>;
export type ListEventsQuery = NonNullable<operations["listEvents"]["parameters"]["query"]>;
export type ListPlanTargetOptionsQuery = NonNullable<operations["listPlanTargetOptions"]["parameters"]["query"]>;

export type ApiPath = keyof paths;

/**
 * Paths that need no session (ADR-0013 section 7). Every other path requires
 * `Authorization: Bearer <token>` and may answer 401 (no or dead session) or
 * 403 (role lacks the permission), both with an `ErrorResponse` body.
 */
export const PUBLIC_API_PATHS: ReadonlySet<ApiPath> = new Set<ApiPath>(["/health", "/ready", "/auth/login"]);

/** True for a 401: no session, or the session expired or was revoked. */
export function isUnauthenticatedError(error: ApiError): boolean {
  return error.status === 401;
}

/** True for a 403: the session is valid but its role lacks the permission. */
export function isPermissionDeniedError(error: ApiError): boolean {
  return error.status === 403;
}

/**
 * A bearer token as RFC 6750 `b64token` (the API issues URL-safe tokens of at
 * least 43 characters). Anything else is never put in a header or a cookie, so
 * a malformed value can never inject a header or cookie attribute.
 */
const BEARER_TOKEN = /^[A-Za-z0-9\-._~+/]{20,512}=*$/;

export function isWellFormedToken(value: unknown): value is string {
  return typeof value === "string" && value.length <= 600 && BEARER_TOKEN.test(value);
}

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
  /**
   * `ErrorResponse.message` of a refused mutation (409 / 422 on a POST), so a
   * reviewer learns WHY (e.g. the span overlaps an existing link). Plain text,
   * truncated; it is the API's own wording and never echoes input (ADR-0013).
   */
  readonly detail?: string;
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
  /**
   * Session bearer token, sent as `Authorization: Bearer <token>` to every
   * non-public path. The caller owns its custody: the web app reads it from an
   * HttpOnly cookie on the server (lib/api.ts); it never reaches browser code.
   * A malformed token is never sent.
   */
  readonly token?: string;
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

  // --- Phase 4 agent orchestration (ADR-0012 section 11): read-only ----------

  /** `GET /agents/runs` — agent review runs, newest first. Contract: 200, 422. */
  listAgentRuns(query?: ListAgentRunsQuery, options?: RequestOptions): Promise<ApiResult<AgentRunList>>;
  /** `GET /agents/runs/{run_id}` — one run with its step timeline and plan reviews. Contract: 200, 404, 422. */
  getAgentRun(runId: string, options?: RequestOptions): Promise<ApiResult<AgentRunDetail>>;
  /**
   * `GET /agents/plans/{plan_id}/reviews` — the agent review(s) of one plan.
   * Contract: 200 (an empty array when no agent reviewed it), 422. No 404.
   */
  listAgentPlanReviews(planId: string, options?: RequestOptions): Promise<ApiResult<AgentPlanReviewList>>;

  // --- Phase 5 reviewer control plane (ADR-0013) ------------------------------
  // Every path below except `POST /auth/login` needs a session; 401 and 403 are
  // `api_error`s (see `isUnauthenticatedError` / `isPermissionDeniedError`).

  /** `POST /auth/login` — exchange credentials for a session. Contract: 200, 401 (any failure), 422. Public. */
  login(body: LoginRequest, options?: RequestOptions): Promise<ApiResult<SessionView>>;
  /** `POST /auth/logout` — revoke the current session. Contract: 204, 401. */
  logout(options?: RequestOptions): Promise<ApiResult<null>>;
  /** `GET /auth/me` — the caller, role and permissions (drives the UI). Contract: 200, 401. */
  getCurrentUser(options?: RequestOptions): Promise<ApiResult<PrincipalView>>;
  /** `GET /dashboard` — the reviewer's home summary (`read`). Contract: 200, 401, 403, 422. */
  getDashboard(options?: RequestOptions): Promise<ApiResult<DashboardSummary>>;
  /** `POST /recommendations/{plan_id}/approve` (`review`). Contract: 200, 401, 403, 404, 409, 422. */
  approvePlan(planId: string, body: ApproveRequest, options?: RequestOptions): Promise<ApiResult<ReviewResult>>;
  /** `POST /recommendations/{plan_id}/reject` (`review`). Contract: 200, 401, 403, 404, 409, 422. */
  rejectPlan(planId: string, body: RejectRequest, options?: RequestOptions): Promise<ApiResult<ReviewResult>>;
  /** `POST /recommendations/{plan_id}/edit-anchor` (`review`): supersede with a new span. Contract: 200, 401, 403, 404, 409, 422. */
  editPlanAnchor(planId: string, body: EditAnchorRequest, options?: RequestOptions): Promise<ApiResult<ReviewResult>>;
  /** `POST /recommendations/{plan_id}/replace-target` (`review`): supersede with a new destination. Contract: 200, 401, 403, 404, 409, 422. */
  replacePlanTarget(
    planId: string,
    body: ReplaceTargetRequest,
    options?: RequestOptions,
  ): Promise<ApiResult<ReviewResult>>;
  /** `POST /recommendations/bulk-review` (`review`): ≤ 50 plans, per-plan results. Contract: 200, 401, 403, 404, 409, 422. */
  bulkReviewPlans(body: BulkReviewRequest, options?: RequestOptions): Promise<ApiResult<BulkReviewResult>>;
  /** `GET /recommendations/{plan_id}/target-options` (`review`): eligible replacement destinations. Contract: 200, 401, 403, 404, 422. */
  listPlanTargetOptions(
    planId: string,
    query?: ListPlanTargetOptionsQuery,
    options?: RequestOptions,
  ): Promise<ApiResult<TargetOptionList>>;
  /** `GET /recommendations/{plan_id}/review-actions` (`read`): the plan's human review history. Contract: 200, 401, 403, 404, 422. */
  listPlanReviewActions(planId: string, options?: RequestOptions): Promise<ApiResult<PlanReviewActionList>>;
  /** `GET /review/actions` (`read_audit`): every review action, newest first. Contract: 200, 401, 403, 422. */
  listReviewActions(query?: ListReviewActionsQuery, options?: RequestOptions): Promise<ApiResult<ReviewActionList>>;
  /** `GET /events` (`read_audit`): the append-only event log, newest first. Contract: 200, 401, 403, 422. */
  listEvents(query?: ListEventsQuery, options?: RequestOptions): Promise<ApiResult<EventList>>;
  /** `GET /settings/policies` (`read_settings`): the active policy documents. Contract: 200, 401, 403, 422. */
  getPolicies(options?: RequestOptions): Promise<ApiResult<PoliciesView>>;
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

// --- Agent guards (Phase 4) ---------------------------------------------------
// Same policy: every field the agent screens read is checked, including nested
// steps, tool calls and reviews; enum membership is left to lib/agents.ts.

export function isToolCallRecord(value: unknown): value is ToolCallRecord {
  return (
    isObject(value) &&
    typeof value["tool"] === "string" &&
    typeof value["permission"] === "string" &&
    typeof value["ok"] === "boolean" &&
    isNumber(value["duration_ms"]) &&
    isNumber(value["attempt"]) &&
    isNullableString(value["error_code"]) &&
    isObject(value["summary"])
  );
}

export function isAgentStepView(value: unknown): value is AgentStepView {
  return (
    isObject(value) &&
    typeof value["run_id"] === "string" &&
    isNumber(value["seq"]) &&
    typeof value["node"] === "string" &&
    typeof value["status"] === "string" &&
    isNumber(value["attempt"]) &&
    typeof value["started_at"] === "string" &&
    typeof value["finished_at"] === "string" &&
    isNumber(value["duration_ms"]) &&
    isNullableString(value["model_role"]) &&
    isNullableString(value["model"]) &&
    typeof value["fallback_used"] === "boolean" &&
    isNullableString(value["fallback_reason"]) &&
    isNullableString(value["error_code"]) &&
    everyItem(value["tool_calls"], isToolCallRecord) &&
    isObject(value["output"]) &&
    typeof value["trace_id"] === "string" &&
    typeof value["span_id"] === "string"
  );
}

export function isAgentPlanReview(value: unknown): value is AgentPlanReview {
  return (
    isObject(value) &&
    typeof value["run_id"] === "string" &&
    typeof value["plan_id"] === "string" &&
    typeof value["verdict"] === "string" &&
    isStringArray(value["reasons"]) &&
    typeof value["explanation"] === "string" &&
    typeof value["handed_off"] === "boolean" &&
    typeof value["fallback_used"] === "boolean" &&
    isNullableString(value["fallback_reason"]) &&
    isNullableString(value["model"]) &&
    typeof value["reviewed_at"] === "string"
  );
}

function hasAgentRunSummaryFields(value: JsonObject): boolean {
  return (
    typeof value["id"] === "string" &&
    typeof value["job_run_id"] === "string" &&
    typeof value["source_page_id"] === "string" &&
    isNullableNumber(value["source_page_version"]) &&
    typeof value["source_url"] === "string" &&
    typeof value["trigger"] === "string" &&
    typeof value["status"] === "string" &&
    isNullableString(value["outcome"]) &&
    isNullableString(value["triage_decision"]) &&
    isStringArray(value["handed_off_plan_ids"]) &&
    typeof value["correlation_id"] === "string" &&
    typeof value["trace_id"] === "string" &&
    typeof value["graph_version"] === "string" &&
    typeof value["started_at"] === "string" &&
    isNullableString(value["finished_at"]) &&
    isNullableString(value["error_code"])
  );
}

export function isAgentRunSummary(value: unknown): value is AgentRunSummary {
  return isObject(value) && hasAgentRunSummaryFields(value);
}

export function isAgentRunDetail(value: unknown): value is AgentRunDetail {
  return (
    isObject(value) &&
    hasAgentRunSummaryFields(value) &&
    everyItem(value["steps"], isAgentStepView) &&
    everyItem(value["reviews"], isAgentPlanReview) &&
    isObject(value["config_snapshot"]) &&
    isStringArray(value["review_notes"])
  );
}

export const isAgentRunList = (value: unknown): value is AgentRunList => isListResponse(value, isAgentRunSummary);
export const isAgentPlanReviewList = (value: unknown): value is AgentPlanReviewList =>
  everyItem(value, isAgentPlanReview);

// --- Control-plane guards (Phase 5) -------------------------------------------
// Same policy: every field the review, audit and settings screens read is
// checked; enum membership is left to lib/review.ts, lib/auth.ts and lib/audit.ts.

export function isPrincipalView(value: unknown): value is PrincipalView {
  return (
    isObject(value) &&
    typeof value["username"] === "string" &&
    typeof value["display_name"] === "string" &&
    typeof value["role"] === "string" &&
    isStringArray(value["permissions"]) &&
    typeof value["session_expires_at"] === "string"
  );
}

export function isSessionView(value: unknown): value is SessionView {
  return (
    isObject(value) &&
    typeof value["token"] === "string" &&
    typeof value["expires_at"] === "string" &&
    isPrincipalView(value["user"])
  );
}

export function isReviewActionView(value: unknown): value is ReviewActionView {
  return (
    isObject(value) &&
    typeof value["id"] === "number" &&
    typeof value["operation_id"] === "string" &&
    typeof value["action"] === "string" &&
    typeof value["actor"] === "string" &&
    typeof value["plan_id"] === "string" &&
    isNullableString(value["new_plan_id"]) &&
    isNullableString(value["new_plan_status"]) &&
    typeof value["from_status"] === "string" &&
    typeof value["to_status"] === "string" &&
    isStringArray(value["reasons"]) &&
    typeof value["note"] === "string" &&
    typeof value["anchor_before"] === "string" &&
    isNullableString(value["anchor_after"]) &&
    typeof value["target_before"] === "string" &&
    isNullableString(value["target_after"]) &&
    isNullableString(value["bulk_operation_id"]) &&
    typeof value["created_at"] === "string"
  );
}

export function isReviewResult(value: unknown): value is ReviewResult {
  return (
    isObject(value) &&
    typeof value["action_id"] === "number" &&
    typeof value["operation_id"] === "string" &&
    typeof value["action"] === "string" &&
    typeof value["plan_id"] === "string" &&
    typeof value["status"] === "string" &&
    isNullableString(value["new_plan_id"]) &&
    isNullableString(value["new_plan_status"]) &&
    typeof value["idempotent_replay"] === "boolean"
  );
}

export function isBulkItemResult(value: unknown): value is BulkItemResult {
  return (
    isObject(value) &&
    typeof value["plan_id"] === "string" &&
    typeof value["ok"] === "boolean" &&
    isNullableString(value["status"]) &&
    isNullableString(value["error_code"])
  );
}

export function isBulkReviewResult(value: unknown): value is BulkReviewResult {
  return (
    isObject(value) &&
    typeof value["operation_id"] === "string" &&
    typeof value["action"] === "string" &&
    everyItem(value["items"], isBulkItemResult) &&
    typeof value["idempotent_replay"] === "boolean"
  );
}

export function isTargetOption(value: unknown): value is TargetOption {
  return (
    isObject(value) &&
    typeof value["page_id"] === "string" &&
    typeof value["canonical_url"] === "string" &&
    isNullableString(value["title"]) &&
    isNullableString(value["page_type"]) &&
    isNullableString(value["market_id"]) &&
    (value["relevance"] === null || isNumber(value["relevance"]))
  );
}

export function isDashboardSummary(value: unknown): value is DashboardSummary {
  return (
    isObject(value) &&
    ["pages_total", "pages_live", "pages_analyzed", "pages_protected", "awaiting_approval"].every((key) =>
      isNumber(value[key]),
    ) &&
    isNumberRecord(value["plans_by_status"]) &&
    isNumberRecord(value["opportunities_by_kind"]) &&
    isNumberRecord(value["agent_runs_by_outcome"]) &&
    isNumberRecord(value["reviews_by_action"]) &&
    everyItem(value["recent_review_actions"], isReviewActionView) &&
    everyItem(value["latest_jobs"], isJobRunSummary) &&
    typeof value["auto_approve_enabled"] === "boolean"
  );
}

export function isEventRecord(value: unknown): value is EventRecord {
  return (
    isObject(value) &&
    typeof value["id"] === "number" &&
    typeof value["event_id"] === "string" &&
    typeof value["event_name"] === "string" &&
    typeof value["event_version"] === "number" &&
    typeof value["occurred_at"] === "string" &&
    typeof value["emitted_by"] === "string" &&
    isNullableString(value["subject_type"]) &&
    isNullableString(value["subject_id"]) &&
    isObject(value["payload"]) &&
    isNullableString(value["correlation_id"])
  );
}

export function isPolicyDocument(value: unknown): value is PolicyDocument {
  return (
    isObject(value) &&
    typeof value["kind"] === "string" &&
    (typeof value["version"] === "number" || typeof value["version"] === "string") &&
    typeof value["path"] === "string" &&
    typeof value["sha256"] === "string" &&
    isObject(value["content"])
  );
}

export function isPoliciesView(value: unknown): value is PoliciesView {
  return (
    isObject(value) &&
    everyItem(value["policies"], isPolicyDocument) &&
    typeof value["auto_approve_enabled"] === "boolean"
  );
}

export const isReviewActionList = (value: unknown): value is ReviewActionList =>
  isListResponse(value, isReviewActionView);
export const isEventList = (value: unknown): value is EventList => isListResponse(value, isEventRecord);
export const isPlanReviewActionList = (value: unknown): value is PlanReviewActionList =>
  everyItem(value, isReviewActionView);
export const isTargetOptionList = (value: unknown): value is TargetOptionList => everyItem(value, isTargetOption);

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
  /** Defaults to GET. A POST carries `body` as JSON. */
  readonly method?: "GET" | "POST";
  readonly body?: unknown;
}

interface ResponseSpec<T> {
  /** Statuses whose body is the success schema. */
  readonly ok: readonly number[];
  /** Error statuses the contract declares for this path; their body is an `ErrorResponse`. */
  readonly declaredErrors: readonly number[];
  readonly guard: (value: unknown) => value is T;
  /** The success status has no body (204); `data` is `null`. */
  readonly empty?: boolean;
}

/** Statuses every non-public path may answer with an `ErrorResponse` (ADR-0013 section 7). */
const SESSION_ERRORS: readonly number[] = [401, 403];

const MAX_DETAIL_CHARS = 300;

function errorMessage(path: ApiPath, status: number): string {
  switch (status) {
    case 401:
      return path === "/auth/login"
        ? "The username or password was not accepted, or the account is temporarily locked."
        : "You are not signed in, or your session has expired.";
    case 403:
      return `Your role does not permit this request (${path}).`;
    case 404:
      return `The API reported that the requested ${path} resource does not exist.`;
    case 409:
      return `The API refused the change to ${path} because it conflicts with the current state (HTTP 409).`;
    case 503:
      return `The API reported that a service needed for ${path} is unavailable (HTTP 503).`;
    default:
      return `The API rejected the request to ${path} (HTTP ${status}).`;
  }
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

const isNull = (value: unknown): value is null => value === null;

async function send<T>(
  base: URL,
  fetchImpl: FetchLike,
  request: ResolvedRequest,
  spec: ResponseSpec<T>,
  timeoutMs: number,
  token: string | undefined,
): Promise<ApiResult<T>> {
  const { path } = request;
  const method = request.method ?? "GET";
  // Preserve any path prefix on the base URL (e.g. behind a reverse proxy).
  const url = new URL(
    request.urlPath.replace(/^\//, ""),
    base.href.endsWith("/") ? base : `${base.href}/`,
  );
  const search = request.query?.toString() ?? "";
  if (search !== "") {
    url.search = search;
  }

  const headers: Record<string, string> = { Accept: "application/json" };
  if (method === "POST") headers["Content-Type"] = "application/json";
  if (token !== undefined) headers["Authorization"] = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetchImpl(url, {
      method,
      headers,
      cache: "no-store",
      // Never follow a redirect with the bearer token attached.
      redirect: "manual",
      signal: AbortSignal.timeout(timeoutMs),
      ...(method === "POST" ? { body: JSON.stringify(request.body ?? {}) } : {}),
    });
  } catch (error) {
    return {
      ok: false,
      error: isAbortTimeout(error)
        ? { kind: "timeout", path, message: `The API did not respond to ${path} in time.` }
        : { kind: "network", path, message: `The API could not be reached for ${path}.` },
    };
  }

  if (spec.empty === true && spec.ok.includes(response.status)) {
    return { ok: true, data: null as T, status: response.status };
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
      const detail =
        method === "POST" && (response.status === 409 || response.status === 422) && body.message.trim() !== ""
          ? truncate(body.message, MAX_DETAIL_CHARS)
          : undefined;
      return {
        ok: false,
        error: {
          kind: "api_error",
          path,
          status: response.status,
          code: body.code,
          message: errorMessage(path, response.status),
          ...(requestId === undefined ? {} : { requestId }),
          ...(detail === undefined ? {} : { detail }),
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
  const token = isWellFormedToken(options.token) ? options.token : undefined;

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
    // Public paths never receive the token; every other path may answer 401/403.
    const isPublic = PUBLIC_API_PATHS.has(request.path);
    const declaredErrors = isPublic
      ? spec.declaredErrors
      : [...new Set([...spec.declaredErrors, ...SESSION_ERRORS])];
    return send(
      base,
      fetchImpl,
      request,
      { ...spec, declaredErrors },
      callOptions.timeoutMs ?? defaultTimeoutMs,
      isPublic ? undefined : token,
    );
  }

  /** A JSON POST (review mutations, login, logout). */
  function post<T>(
    path: ApiPath,
    urlPath: string,
    body: unknown,
    spec: ResponseSpec<T>,
    callOptions: RequestOptions,
  ): Promise<ApiResult<T>> {
    return call({ path, urlPath, method: "POST", body }, spec, callOptions);
  }

  const REVIEW_ERRORS = [404, 409, 422] as const;

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

    listAgentRuns: (query, callOptions = {}) =>
      inventory("/agents/runs", "/agents/runs", isAgentRunList, query, callOptions, [422]),

    getAgentRun: (runId, callOptions = {}) => {
      const path = "/agents/runs/{run_id}";
      if (!isSafePathId(runId)) return invalidArgument(path, "agent run id");
      return inventory(path, `/agents/runs/${id(runId)}`, isAgentRunDetail, undefined, callOptions);
    },

    listAgentPlanReviews: (planId, callOptions = {}) => {
      const path = "/agents/plans/{plan_id}/reviews";
      if (!isSafePathId(planId)) return invalidArgument(path, "recommendation id");
      return inventory(
        path,
        `/agents/plans/${id(planId)}/reviews`,
        isAgentPlanReviewList,
        undefined,
        callOptions,
        [422],
      );
    },

    login: (body, callOptions = {}) =>
      post("/auth/login", "/auth/login", body, { ok: [200], declaredErrors: [401, 422], guard: isSessionView }, callOptions),

    logout: (callOptions = {}) =>
      post("/auth/logout", "/auth/logout", {}, { ok: [204], declaredErrors: [], guard: isNull, empty: true }, callOptions),

    getCurrentUser: (callOptions = {}) =>
      inventory("/auth/me", "/auth/me", isPrincipalView, undefined, callOptions, []),

    getDashboard: (callOptions = {}) =>
      inventory("/dashboard", "/dashboard", isDashboardSummary, undefined, callOptions, [422]),

    approvePlan: (planId, body, callOptions = {}) => {
      const path = "/recommendations/{plan_id}/approve";
      if (!isSafePathId(planId)) return invalidArgument(path, "recommendation id");
      return post(path, `/recommendations/${id(planId)}/approve`, body, { ok: [200], declaredErrors: REVIEW_ERRORS, guard: isReviewResult }, callOptions);
    },

    rejectPlan: (planId, body, callOptions = {}) => {
      const path = "/recommendations/{plan_id}/reject";
      if (!isSafePathId(planId)) return invalidArgument(path, "recommendation id");
      return post(path, `/recommendations/${id(planId)}/reject`, body, { ok: [200], declaredErrors: REVIEW_ERRORS, guard: isReviewResult }, callOptions);
    },

    editPlanAnchor: (planId, body, callOptions = {}) => {
      const path = "/recommendations/{plan_id}/edit-anchor";
      if (!isSafePathId(planId)) return invalidArgument(path, "recommendation id");
      return post(path, `/recommendations/${id(planId)}/edit-anchor`, body, { ok: [200], declaredErrors: REVIEW_ERRORS, guard: isReviewResult }, callOptions);
    },

    replacePlanTarget: (planId, body, callOptions = {}) => {
      const path = "/recommendations/{plan_id}/replace-target";
      if (!isSafePathId(planId)) return invalidArgument(path, "recommendation id");
      return post(path, `/recommendations/${id(planId)}/replace-target`, body, { ok: [200], declaredErrors: REVIEW_ERRORS, guard: isReviewResult }, callOptions);
    },

    bulkReviewPlans: (body, callOptions = {}) =>
      post(
        "/recommendations/bulk-review",
        "/recommendations/bulk-review",
        body,
        { ok: [200], declaredErrors: REVIEW_ERRORS, guard: isBulkReviewResult },
        callOptions,
      ),

    listPlanTargetOptions: (planId, query, callOptions = {}) => {
      const path = "/recommendations/{plan_id}/target-options";
      if (!isSafePathId(planId)) return invalidArgument(path, "recommendation id");
      return inventory(path, `/recommendations/${id(planId)}/target-options`, isTargetOptionList, query, callOptions);
    },

    listPlanReviewActions: (planId, callOptions = {}) => {
      const path = "/recommendations/{plan_id}/review-actions";
      if (!isSafePathId(planId)) return invalidArgument(path, "recommendation id");
      return inventory(path, `/recommendations/${id(planId)}/review-actions`, isPlanReviewActionList, undefined, callOptions);
    },

    listReviewActions: (query, callOptions = {}) =>
      inventory("/review/actions", "/review/actions", isReviewActionList, query, callOptions, [422]),

    listEvents: (query, callOptions = {}) => inventory("/events", "/events", isEventList, query, callOptions, [422]),

    getPolicies: (callOptions = {}) =>
      inventory("/settings/policies", "/settings/policies", isPoliciesView, undefined, callOptions, [422]),
  };
}
