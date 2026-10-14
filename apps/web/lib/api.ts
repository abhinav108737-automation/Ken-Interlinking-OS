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
 * Functions never throw for an API failure. They return an `ApiResult`, so a
 * page can always render either the data or a meaningful error state.
 *
 * SESSION CUSTODY (ADR-0013 sections 2 and 7). This is also the ONE module that
 * reads and writes the session cookie. Every call to a non-public path
 * forwards the cookie's token as `Authorization: Bearer <token>`, so no page
 * can forget it. When the API answers 401, the call does not return: it
 * redirects (a Next.js control-flow throw) to `/auth/expired`, which clears a
 * cookie the API no longer accepts and continues to `/login?next=<this page>`.
 * Authorisation itself is always the API's decision, never this module's.
 */
import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import { cache } from "react";

import {
  type AgentPlanReviewList,
  type AgentRunDetail,
  type AgentRunList,
  type ApiClient,
  type ApiPath,
  type ApiResult,
  type CandidateList,
  type HealthResponse,
  type IngestionRunDetail,
  type IntelligenceCoverage,
  type IssueList,
  type JobRunDetail,
  type JobRunList,
  type LinkList,
  type ListAgentRunsQuery,
  type LinkPlanDetail,
  type LinkPlanList,
  type ListIngestionRunIssuesQuery,
  type ListIngestionRunsQuery,
  type ListInventoryLinksQuery,
  type ListInventoryPageLinksQuery,
  type ListInventoryPageVersionsQuery,
  type ListInventoryPagesQuery,
  type ListJobRunsQuery,
  type ListLinkCandidatesQuery,
  type ListLinkPlansQuery,
  type ListOpportunitiesQuery,
  type ListPageRelationshipsQuery,
  type ListSimilarPagesQuery,
  type ListTaxonomyNodePagesQuery,
  type MetaResponse,
  type OpportunityList,
  type PageDetail,
  type PageIntelligenceView,
  type PageList,
  type PageVersionDetail,
  type PageVersionList,
  type ReadinessResponse,
  type RecommendationSummary,
  type RelationshipList,
  type RequestOptions,
  type RunList,
  type SemanticSearchQuery,
  type SimilarPageList,
  type TaxonomyView,
  type ApproveRequest,
  type BulkReviewRequest,
  type BulkReviewResult,
  type DashboardSummary,
  type EditAnchorRequest,
  type EventList,
  type ListEventsQuery,
  type ListPlanTargetOptionsQuery,
  type ListReviewActionsQuery,
  type LoginRequest,
  type PlanReviewActionList,
  type PoliciesView,
  type PrincipalView,
  type RejectRequest,
  type ReplaceTargetRequest,
  type ReviewActionList,
  type ReviewResult,
  type SessionView,
  type TargetOptionList,
  DEFAULT_API_BASE_URL,
  PUBLIC_API_PATHS,
  configError,
  createApiClient,
  isUnauthenticatedError,
  isWellFormedToken,
  parseBaseUrl,
} from "./api-client";
import {
  REQUEST_PATH_HEADER,
  SESSION_COOKIE,
  clearedSessionCookieOptions,
  loginHref,
  safeNextPath,
  sessionCookieOptions,
  sessionExpiredHref,
  sessionMaxAgeSeconds,
  shouldUseSecureCookie,
} from "./session";

export type {
  AgentErrorCode,
  AgentNode,
  AgentOutcome,
  AgentPlanReview,
  AgentPlanReviewList,
  AgentRunDetail,
  AgentRunList,
  AgentRunStatus,
  AgentRunSummary,
  AgentStepView,
  ApiClient,
  ApiClientOptions,
  ApiError,
  ApiErrorKind,
  ApiPath,
  ApiResult,
  CandidateList,
  CandidateView,
  DependencyCheck,
  ErrorCode,
  ExistingLinkView,
  FetchLike,
  HealthResponse,
  IngestionIssueView,
  IngestionRunDetail,
  IngestionRunSummary,
  IntelligenceCoverage,
  IssueList,
  JobRunDetail,
  JobRunList,
  JobRunSummary,
  LinkList,
  LinkPlan,
  LinkPlanDetail,
  LinkPlanList,
  LinkPlanStatus,
  MetaResponse,
  OpportunityKind,
  OpportunityList,
  OpportunityView,
  PageDetail,
  PageInfo,
  PageIntelligenceView,
  PageRelationshipView,
  PageList,
  PageSummary,
  PageVersionDetail,
  PageVersionList,
  PageVersionSummary,
  ReadinessResponse,
  RecommendationSummary,
  RelationshipList,
  RequestOptions,
  RunList,
  ServiceIdentity,
  SimilarPage,
  SimilarPageList,
  TaxonomyView,
  ToolCallRecord,
  ApproveRequest,
  BulkAction,
  BulkItemResult,
  BulkReviewRequest,
  BulkReviewResult,
  DashboardSummary,
  EditAnchorRequest,
  EventList,
  EventName,
  EventRecord,
  FeedbackReason,
  Permission,
  PlanReviewActionList,
  PoliciesView,
  PolicyDocument,
  PrincipalView,
  RejectRequest,
  ReplaceTargetRequest,
  ReviewAction,
  ReviewActionList,
  ReviewActionView,
  ReviewResult,
  Role,
  TargetOption,
  TargetOptionList,
} from "./api-client";
export {
  DEFAULT_API_BASE_URL,
  DEFAULT_TIMEOUT_MS,
  createApiClient,
  isHealthResponse,
  isMetaResponse,
  isMissingResourceError,
  isModelUnavailableError,
  isNotFoundError,
  isPermissionDeniedError,
  isReadinessResponse,
  isUnauthenticatedError,
} from "./api-client";

/** The configured API base URL, or `null` when `KEN_API_BASE_URL` is not a valid http(s) URL. */
export function resolveBaseUrl(): URL | null {
  return parseBaseUrl(process.env["KEN_API_BASE_URL"]?.trim() || DEFAULT_API_BASE_URL);
}

// ---------------------------------------------------------------------------
// Session custody: the only reads and writes of the session cookie.
// ---------------------------------------------------------------------------

/** The session token from the request's cookie, when present and well formed. */
async function sessionToken(): Promise<string | undefined> {
  const value = (await cookies()).get(SESSION_COOKIE)?.value;
  return isWellFormedToken(value) ? value : undefined;
}

/** The requested path (set by proxy.ts), made safe for a `next` parameter. */
async function requestedPath(): Promise<string> {
  return safeNextPath((await headers()).get(REQUEST_PATH_HEADER));
}

/** Whether this request's cookie must carry `Secure` (see `shouldUseSecureCookie`). */
async function secureCookie(): Promise<boolean> {
  const requestHeaders = await headers();
  return shouldUseSecureCookie(requestHeaders.get("host"), requestHeaders.get("x-forwarded-proto"));
}

/**
 * Store a new session in the HttpOnly cookie. Server actions and route handlers
 * only (Next.js cannot set cookies while rendering). Returns `false`, storing
 * nothing, when the token is malformed or the session is already expired.
 */
export async function startSession(session: SessionView, nowMs: number = Date.now()): Promise<boolean> {
  const maxAge = sessionMaxAgeSeconds(session.expires_at, nowMs);
  if (maxAge === null || !isWellFormedToken(session.token)) return false;
  (await cookies()).set(SESSION_COOKIE, session.token, sessionCookieOptions(maxAge, await secureCookie()));
  return true;
}

/** Delete the session cookie. Server actions and route handlers only. */
export async function endSession(): Promise<void> {
  (await cookies()).set(SESSION_COOKIE, "", clearedSessionCookieOptions(await secureCookie()));
}

/** True when the request carries a (well-formed) session cookie. Says nothing about validity. */
export async function hasSessionCookie(): Promise<boolean> {
  return (await sessionToken()) !== undefined;
}

/**
 * Leave the current request after an API 401: to `/auth/expired` (which clears
 * the dead cookie) when one was sent, otherwise straight to `/login`. Both keep
 * a safe `next` back to the requested page. Never returns.
 */
async function leaveUnauthenticated(sentToken: boolean): Promise<never> {
  const next = await requestedPath();
  redirect(sentToken ? sessionExpiredHref(next) : loginHref(next));
}

interface ServerCallOptions {
  /** Default true: a 401 redirects to sign-in. False returns the 401 as a result. */
  readonly redirectOnUnauthenticated?: boolean;
}

// Resolved per call (not at import) so the environment is read at request time.
async function withServerClient<T>(
  path: ApiPath,
  run: (client: ApiClient) => Promise<ApiResult<T>>,
  { redirectOnUnauthenticated = true }: ServerCallOptions = {},
): Promise<ApiResult<T>> {
  const baseUrl = resolveBaseUrl();
  if (baseUrl === null) {
    return configError<T>(path, "KEN_API_BASE_URL is not a valid http(s) URL.");
  }
  const isPublic = PUBLIC_API_PATHS.has(path);
  const token = isPublic ? undefined : await sessionToken();
  const result = await run(createApiClient({ baseUrl, ...(token === undefined ? {} : { token }) }));
  if (!isPublic && redirectOnUnauthenticated && !result.ok && isUnauthenticatedError(result.error)) {
    return leaveUnauthenticated(token !== undefined);
  }
  return result;
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

// ---------------------------------------------------------------------------
// Phase 2 content intelligence and job runs (ADR-0010 sections 6-7). All
// read-only GETs; analysis runs start from the CLI, never from here.
// ---------------------------------------------------------------------------

/** `GET /intelligence/coverage` — analysis/embedding/eligibility coverage. Contract: 200 only. */
export function getIntelligenceCoverage(options: RequestOptions = {}): Promise<ApiResult<IntelligenceCoverage>> {
  return withServerClient("/intelligence/coverage", (client) => client.getIntelligenceCoverage(options));
}

/** `GET /intelligence/taxonomy` — the active taxonomy with page counts. Contract: 200 only. */
export function getTaxonomy(options: RequestOptions = {}): Promise<ApiResult<TaxonomyView>> {
  return withServerClient("/intelligence/taxonomy", (client) => client.getTaxonomy(options));
}

/** `GET /intelligence/taxonomy/{node_id}/pages` — pages for one node. Contract: 200, 404, 422. */
export function listTaxonomyNodePages(
  nodeId: string,
  query: ListTaxonomyNodePagesQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<SimilarPageList>> {
  return withServerClient("/intelligence/taxonomy/{node_id}/pages", (client) =>
    client.listTaxonomyNodePages(nodeId, query, options),
  );
}

/** `GET /intelligence/pages/{page_id}` — one page's intelligence. Contract: 200, 404, 422. */
export function getPageIntelligence(
  pageId: string,
  options: RequestOptions = {},
): Promise<ApiResult<PageIntelligenceView>> {
  return withServerClient("/intelligence/pages/{page_id}", (client) => client.getPageIntelligence(pageId, options));
}

/** `GET /intelligence/pages/{page_id}/similar` — nearest pages by embedding. Contract: 200, 404, 422. */
export function listSimilarPages(
  pageId: string,
  query: ListSimilarPagesQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<SimilarPageList>> {
  return withServerClient("/intelligence/pages/{page_id}/similar", (client) =>
    client.listSimilarPages(pageId, query, options),
  );
}

/** `GET /intelligence/pages/{page_id}/relationships` — typed edges. Contract: 200, 404, 422. */
export function listPageRelationships(
  pageId: string,
  query: ListPageRelationshipsQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<RelationshipList>> {
  return withServerClient("/intelligence/pages/{page_id}/relationships", (client) =>
    client.listPageRelationships(pageId, query, options),
  );
}

/**
 * `GET /intelligence/search` — semantic search. Contract: 200, 422, 503. A 503
 * is an `api_error` with code `model_unavailable` (`isModelUnavailableError`).
 */
export function semanticSearch(
  query: SemanticSearchQuery,
  options: RequestOptions = {},
): Promise<ApiResult<SimilarPageList>> {
  return withServerClient("/intelligence/search", (client) => client.semanticSearch(query, options));
}

/** `GET /jobs/runs` — batch job runs. Contract: 200, 422. */
export function listJobRuns(query: ListJobRunsQuery = {}, options: RequestOptions = {}): Promise<ApiResult<JobRunList>> {
  return withServerClient("/jobs/runs", (client) => client.listJobRuns(query, options));
}

/** `GET /jobs/runs/{run_id}` — one job run. Contract: 200, 404, 422. */
export function getJobRun(runId: string, options: RequestOptions = {}): Promise<ApiResult<JobRunDetail>> {
  return withServerClient("/jobs/runs/{run_id}", (client) => client.getJobRun(runId, options));
}

// ---------------------------------------------------------------------------
// Phase 3 recommendations (ADR-0011 section 8). All read-only GETs; the engine
// runs from the CLI (`make recommend`), and review actions arrive in Phase 5.
// ---------------------------------------------------------------------------

/** `GET /recommendations` — link plans, highest score first. Contract: 200, 422. */
export function listLinkPlans(
  query: ListLinkPlansQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<LinkPlanList>> {
  return withServerClient("/recommendations", (client) => client.listLinkPlans(query, options));
}

/** `GET /recommendations/{plan_id}` — one plan with its block text and history. Contract: 200, 404, 422. */
export function getLinkPlan(planId: string, options: RequestOptions = {}): Promise<ApiResult<LinkPlanDetail>> {
  return withServerClient("/recommendations/{plan_id}", (client) => client.getLinkPlan(planId, options));
}

/** `GET /recommendations/candidates` — retrieved candidates, kept or rejected. Contract: 200, 422. */
export function listLinkCandidates(
  query: ListLinkCandidatesQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<CandidateList>> {
  return withServerClient("/recommendations/candidates", (client) => client.listLinkCandidates(query, options));
}

/** `GET /recommendations/opportunities` — orphan, under-linked and capacity pages. Contract: 200, 422. */
export function listOpportunities(
  query: ListOpportunitiesQuery = {},
  options: RequestOptions = {},
): Promise<ApiResult<OpportunityList>> {
  return withServerClient("/recommendations/opportunities", (client) => client.listOpportunities(query, options));
}

/** `GET /recommendations/summary` — dashboard counts. Contract: 200 only. */
export function getRecommendationSummary(options: RequestOptions = {}): Promise<ApiResult<RecommendationSummary>> {
  return withServerClient("/recommendations/summary", (client) => client.getRecommendationSummary(options));
}

// ---------------------------------------------------------------------------
// Phase 4 agent orchestration (ADR-0012 section 11). All read-only GETs; agent
// runs start from the CLI (`make agents`), never from this console.
// ---------------------------------------------------------------------------

/** `GET /agents/runs` — agent review runs, newest first. Contract: 200, 422. */
export function listAgentRuns(query: ListAgentRunsQuery = {}, options: RequestOptions = {}): Promise<ApiResult<AgentRunList>> {
  return withServerClient("/agents/runs", (client) => client.listAgentRuns(query, options));
}

/** `GET /agents/runs/{run_id}` — one run with its steps and plan reviews. Contract: 200, 404, 422. */
export function getAgentRun(runId: string, options: RequestOptions = {}): Promise<ApiResult<AgentRunDetail>> {
  return withServerClient("/agents/runs/{run_id}", (client) => client.getAgentRun(runId, options));
}

/** `GET /agents/plans/{plan_id}/reviews` — agent review(s) of one plan; empty if none. Contract: 200, 422. */
export function listAgentPlanReviews(
  planId: string,
  options: RequestOptions = {},
): Promise<ApiResult<AgentPlanReviewList>> {
  return withServerClient("/agents/plans/{plan_id}/reviews", (client) => client.listAgentPlanReviews(planId, options));
}

// ---------------------------------------------------------------------------
// Phase 5 reviewer control plane (ADR-0013). Mutations are called ONLY from
// server actions; each carries a client-generated operation_id.
// ---------------------------------------------------------------------------

/** Login and review calls may wait on scrypt or a per-plan transaction. */
const MUTATION_TIMEOUT_MS = 15_000;
/** Bulk review runs ≤ 50 transactions sequentially. */
const BULK_TIMEOUT_MS = 60_000;

/** `POST /auth/login`. Public; a 401 is returned (wrong credentials), never redirected. */
export function login(body: LoginRequest): Promise<ApiResult<SessionView>> {
  return withServerClient("/auth/login", (client) => client.login(body, { timeoutMs: MUTATION_TIMEOUT_MS }));
}

/** `POST /auth/logout`. A 401 (already expired) is returned, never redirected. */
export function logout(): Promise<ApiResult<null>> {
  return withServerClient("/auth/logout", (client) => client.logout(), { redirectOnUnauthenticated: false });
}

/**
 * `GET /auth/me` without redirecting on 401 (for the layout and the sign-in
 * screens, which must render signed-out). Deduplicated per request.
 */
export const getSessionUser = cache(
  (): Promise<ApiResult<PrincipalView>> =>
    withServerClient("/auth/me", (client) => client.getCurrentUser(), { redirectOnUnauthenticated: false }),
);

/**
 * `GET /auth/me` for a page that needs a session: a 401 redirects to sign-in.
 * Shares the per-request result of `getSessionUser`.
 */
export async function requireSessionUser(): Promise<ApiResult<PrincipalView>> {
  const result = await getSessionUser();
  if (!result.ok && isUnauthenticatedError(result.error)) {
    return leaveUnauthenticated(await hasSessionCookie());
  }
  return result;
}

/** `GET /dashboard`. */
export function getDashboard(): Promise<ApiResult<DashboardSummary>> {
  return withServerClient("/dashboard", (client) => client.getDashboard());
}

/** `POST /recommendations/{plan_id}/approve`. */
export function approvePlan(planId: string, body: ApproveRequest): Promise<ApiResult<ReviewResult>> {
  return withServerClient("/recommendations/{plan_id}/approve", (client) =>
    client.approvePlan(planId, body, { timeoutMs: MUTATION_TIMEOUT_MS }),
  );
}

/** `POST /recommendations/{plan_id}/reject`. */
export function rejectPlan(planId: string, body: RejectRequest): Promise<ApiResult<ReviewResult>> {
  return withServerClient("/recommendations/{plan_id}/reject", (client) =>
    client.rejectPlan(planId, body, { timeoutMs: MUTATION_TIMEOUT_MS }),
  );
}

/** `POST /recommendations/{plan_id}/edit-anchor`. */
export function editPlanAnchor(planId: string, body: EditAnchorRequest): Promise<ApiResult<ReviewResult>> {
  return withServerClient("/recommendations/{plan_id}/edit-anchor", (client) =>
    client.editPlanAnchor(planId, body, { timeoutMs: MUTATION_TIMEOUT_MS }),
  );
}

/** `POST /recommendations/{plan_id}/replace-target`. */
export function replacePlanTarget(planId: string, body: ReplaceTargetRequest): Promise<ApiResult<ReviewResult>> {
  return withServerClient("/recommendations/{plan_id}/replace-target", (client) =>
    client.replacePlanTarget(planId, body, { timeoutMs: MUTATION_TIMEOUT_MS }),
  );
}

/** `POST /recommendations/bulk-review`. */
export function bulkReviewPlans(body: BulkReviewRequest): Promise<ApiResult<BulkReviewResult>> {
  return withServerClient("/recommendations/bulk-review", (client) =>
    client.bulkReviewPlans(body, { timeoutMs: BULK_TIMEOUT_MS }),
  );
}

/** `GET /recommendations/{plan_id}/target-options`. */
export function listPlanTargetOptions(
  planId: string,
  query: ListPlanTargetOptionsQuery = {},
): Promise<ApiResult<TargetOptionList>> {
  return withServerClient("/recommendations/{plan_id}/target-options", (client) =>
    client.listPlanTargetOptions(planId, query),
  );
}

/** `GET /recommendations/{plan_id}/review-actions`. */
export function listPlanReviewActions(planId: string): Promise<ApiResult<PlanReviewActionList>> {
  return withServerClient("/recommendations/{plan_id}/review-actions", (client) =>
    client.listPlanReviewActions(planId),
  );
}

/** `GET /review/actions`. */
export function listReviewActions(query: ListReviewActionsQuery = {}): Promise<ApiResult<ReviewActionList>> {
  return withServerClient("/review/actions", (client) => client.listReviewActions(query));
}

/** `GET /events`. */
export function listEvents(query: ListEventsQuery = {}): Promise<ApiResult<EventList>> {
  return withServerClient("/events", (client) => client.listEvents(query));
}

/** `GET /settings/policies`. */
export function getPolicies(): Promise<ApiResult<PoliciesView>> {
  return withServerClient("/settings/policies", (client) => client.getPolicies());
}
