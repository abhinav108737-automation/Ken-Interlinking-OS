// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as serverApi from "@/lib/api";
import {
  type ApiClient,
  type ApiResult,
  type FetchLike,
  createApiClient,
  isCandidateView,
  isLinkPlan,
  isLinkPlanDetail,
  isMissingResourceError,
  isNotFoundError,
  isOpportunityView,
  isPlacementLocator,
  isPlacementPreview,
  isRecommendationSummary,
  isScoreBreakdown,
} from "@/lib/api-client";

import { OTHER_PAGE_ID, PAGE_ID, errorBody, jsonResponse, list } from "./fixtures/inventory";
import {
  PLAN_ID,
  REC_RUN_ID,
  candidate,
  linkPlan,
  linkPlanDetail,
  opportunity,
  recommendationSummary,
} from "./fixtures/recommendations";

const BASE = "http://api.test:8000/prefix";

let fetchMock: ReturnType<typeof vi.fn<FetchLike>>;
let client: ApiClient;

beforeEach(() => {
  fetchMock = vi.fn<FetchLike>();
  client = createApiClient({ baseUrl: BASE, fetch: fetchMock });
});

function requestedUrl(call = 0): URL {
  const args = fetchMock.mock.calls[call];
  if (args === undefined) throw new Error("fetch was not called");
  return new URL(String(args[0]));
}

function expectError(result: ApiResult<unknown>) {
  expect(result.ok).toBe(false);
  if (result.ok) throw new Error("unreachable");
  return result.error;
}

const OPERATIONS: ReadonlyArray<{
  name: string;
  template: string;
  declared: readonly number[];
  call: (c: ApiClient) => Promise<ApiResult<unknown>>;
  body: unknown;
  expectedPath: string;
}> = [
  {
    name: "listLinkPlans",
    template: "/recommendations",
    declared: [422],
    call: (c) => c.listLinkPlans(),
    body: list([linkPlan()]),
    expectedPath: "/prefix/recommendations",
  },
  {
    name: "getLinkPlan",
    template: "/recommendations/{plan_id}",
    declared: [404, 422],
    call: (c) => c.getLinkPlan(PLAN_ID),
    body: linkPlanDetail(),
    expectedPath: `/prefix/recommendations/${PLAN_ID}`,
  },
  {
    name: "listLinkCandidates",
    template: "/recommendations/candidates",
    declared: [422],
    call: (c) => c.listLinkCandidates(),
    body: list([candidate(), candidate({ rejection: "already_linked", plan_id: null, total_score: null })]),
    expectedPath: "/prefix/recommendations/candidates",
  },
  {
    name: "listOpportunities",
    template: "/recommendations/opportunities",
    declared: [422],
    call: (c) => c.listOpportunities(),
    body: list([opportunity()]),
    expectedPath: "/prefix/recommendations/opportunities",
  },
  {
    name: "getRecommendationSummary",
    template: "/recommendations/summary",
    declared: [],
    call: (c) => c.getRecommendationSummary(),
    body: recommendationSummary(),
    expectedPath: "/prefix/recommendations/summary",
  },
];

describe.each(OPERATIONS)("$name", (op) => {
  it("GETs the right URL and parses a 200", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(op.body, 200));
    expect(await op.call(client)).toEqual({ ok: true, data: op.body, status: 200 });
    const url = requestedUrl();
    expect(url.origin).toBe("http://api.test:8000");
    expect(url.pathname).toBe(op.expectedPath);
    expect(url.search).toBe("");
    const init = fetchMock.mock.calls[0]![1];
    expect(init.method).toBe("GET");
    expect(init.cache).toBe("no-store");
  });

  it(op.declared.includes(422) ? "maps a declared 422 to api_error" : "treats an undeclared 422 as http_status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("validation_failed"), 422));
    const error = expectError(await op.call(client));
    expect(error.path).toBe(op.template);
    expect(error.kind).toBe(op.declared.includes(422) ? "api_error" : "http_status");
    expect(error.code).toBe("validation_failed");
  });

  it(op.declared.includes(404) ? "maps a declared 404 to not-found" : "treats an undeclared 404 as http_status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("not_found", null), 404));
    const error = expectError(await op.call(client));
    expect(isNotFoundError(error)).toBe(op.declared.includes(404));
    expect(error.kind).toBe(op.declared.includes(404) ? "api_error" : "http_status");
  });

  it("rejects a 200 body that misses the contract shape", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: "nope", page: {} }, 200));
    expect(expectError(await op.call(client))).toMatchObject({ kind: "invalid_body", status: 200 });
  });

  it("reports network failures and timeouts without throwing", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError(`fetch failed for ${BASE}`));
    const network = expectError(await op.call(client));
    expect(network.kind).toBe("network");
    expect(network.message).not.toContain("api.test");
    fetchMock.mockRejectedValueOnce(new DOMException("timed out", "TimeoutError"));
    expect(expectError(await op.call(client)).kind).toBe("timeout");
  });
});

describe("query construction", () => {
  it("encodes each operation's parameters and omits absent ones", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([]), 200));
    await client.listLinkPlans({
      status: "awaiting_approval",
      min_score: 0.5,
      source_page_id: PAGE_ID,
      target_page_id: OTHER_PAGE_ID,
      limit: 20,
      offset: 40,
    });
    await client.listLinkPlans({ status: null, min_score: null, source_page_id: null, target_page_id: null });
    await client.listLinkCandidates({ run_id: REC_RUN_ID, source_page_id: PAGE_ID, rejected: false });
    await client.listLinkCandidates({ rejected: true, limit: 5 });
    await client.listOpportunities({ kind: "link_capacity", offset: 50 });
    expect(requestedUrl(0).search).toBe(
      `?status=awaiting_approval&min_score=0.5&source_page_id=${PAGE_ID}&target_page_id=${OTHER_PAGE_ID}&limit=20&offset=40`,
    );
    expect(requestedUrl(1).search).toBe("");
    expect(requestedUrl(2).search).toBe(`?run_id=${REC_RUN_ID}&source_page_id=${PAGE_ID}&rejected=false`);
    expect(requestedUrl(3).search).toBe("?rejected=true&limit=5");
    expect(requestedUrl(4).search).toBe("?kind=link_capacity&offset=50");
  });

  it.each(["", ".", "..", "%2e%2e", "a/b", "a?b", "a#b", "a b", "x".repeat(65)])(
    "refuses plan id %j without sending a request",
    async (bad) => {
      const error = expectError(await client.getLinkPlan(bad));
      expect(error).toMatchObject({ kind: "invalid_argument", path: "/recommendations/{plan_id}" });
      expect(isMissingResourceError(error)).toBe(true);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );
});

describe("recommendation guards", () => {
  it("accept the fixtures, including nullable fields", () => {
    expect(isLinkPlan(linkPlan())).toBe(true);
    expect(isLinkPlan(linkPlan({ relationship_type: null, run_id: null, embedding_model: null, supersedes: PLAN_ID }))).toBe(true);
    expect(isLinkPlanDetail(linkPlanDetail({ source_title: null, transitions: [] }))).toBe(true);
    expect(isCandidateView(candidate({ rejection: "self_link", plan_id: null, total_score: null }))).toBe(true);
    expect(isOpportunityView(opportunity())).toBe(true);
    expect(isRecommendationSummary(recommendationSummary({ last_run_id: null }))).toBe(true);
  });

  it("reject a malformed score breakdown", () => {
    const score = linkPlan().score;
    expect(isScoreBreakdown(score)).toBe(true);
    expect(isScoreBreakdown({ ...score, components: [{ name: "semantic", value: "0.8", weight: 0.5, contribution: 0.4 }] })).toBe(false);
    expect(isScoreBreakdown({ ...score, components: [{ value: 0.8, weight: 0.5, contribution: 0.4 }] })).toBe(false);
    expect(isScoreBreakdown({ ...score, factors: { placement_quality: "high" } })).toBe(false);
    expect(isScoreBreakdown({ ...score, factors: null })).toBe(false);
    expect(isScoreBreakdown({ ...score, semantic_floor: undefined })).toBe(false);
    expect(isScoreBreakdown({ ...score, total: null })).toBe(false);
    expect(isLinkPlan({ ...linkPlan(), score: { ...score, policy_name: 1 } })).toBe(false);
  });

  it("reject a malformed locator or preview", () => {
    const { locator, preview } = linkPlan();
    expect(isPlacementLocator(locator)).toBe(true);
    expect(isPlacementLocator({ ...locator, start: "11" })).toBe(false);
    expect(isPlacementLocator({ ...locator, start: -1 })).toBe(false);
    expect(isPlacementLocator({ ...locator, start: 1.5 })).toBe(false);
    expect(isPlacementLocator({ ...locator, end: locator.start })).toBe(false);
    expect(isPlacementLocator({ ...locator, context_hash: undefined })).toBe(false);
    expect(isPlacementPreview(preview)).toBe(true);
    expect(isPlacementPreview({ ...preview, anchor: null })).toBe(false);
    expect(isLinkPlan({ ...linkPlan(), preview: { before: "a", after: "b" } })).toBe(false);
    expect(isLinkPlan({ ...linkPlan(), locator: null })).toBe(false);
  });

  it("reject malformed plan fields, history and summaries", () => {
    expect(isLinkPlan({ ...linkPlan(), reason_codes: "same_market" })).toBe(false);
    expect(isLinkPlan({ ...linkPlan(), confidence: "0.6" })).toBe(false);
    expect(isLinkPlan({ ...linkPlan(), calibration_version: 1 })).toBe(false);
    expect(isLinkPlanDetail(linkPlan())).toBe(false);
    expect(isLinkPlanDetail(linkPlanDetail({ transitions: [{ id: 1 }] as never }))).toBe(false);
    expect(isCandidateView({ ...candidate(), channel_scores: { semantic: "1" } })).toBe(false);
    expect(isOpportunityView({ ...opportunity(), evidence: [] })).toBe(false);
    expect(isRecommendationSummary({ ...recommendationSummary(), plans_by_status: { proposed: "1" } })).toBe(false);
  });
});

describe("server wrappers (lib/api.ts)", () => {
  const globalFetch = vi.fn<typeof fetch>();

  beforeEach(() => {
    globalFetch.mockReset();
    vi.stubGlobal("fetch", globalFetch);
    vi.stubEnv("KEN_API_BASE_URL", "http://server.test:9000");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("resolves KEN_API_BASE_URL per call and forwards ids and queries", async () => {
    globalFetch
      .mockResolvedValueOnce(jsonResponse(list([linkPlan()]), 200))
      .mockResolvedValueOnce(jsonResponse(linkPlanDetail(), 200))
      .mockResolvedValueOnce(jsonResponse(list([candidate()]), 200))
      .mockResolvedValueOnce(jsonResponse(list([opportunity()]), 200))
      .mockResolvedValueOnce(jsonResponse(recommendationSummary(), 200));
    expect((await serverApi.listLinkPlans({ status: "proposed", limit: 10 })).ok).toBe(true);
    expect((await serverApi.getLinkPlan(PLAN_ID)).ok).toBe(true);
    expect((await serverApi.listLinkCandidates({ source_page_id: PAGE_ID })).ok).toBe(true);
    expect((await serverApi.listOpportunities({ kind: "orphan" })).ok).toBe(true);
    expect((await serverApi.getRecommendationSummary()).ok).toBe(true);
    expect(globalFetch.mock.calls.map(([url]) => String(url))).toEqual([
      "http://server.test:9000/recommendations?status=proposed&limit=10",
      `http://server.test:9000/recommendations/${PLAN_ID}`,
      `http://server.test:9000/recommendations/candidates?source_page_id=${PAGE_ID}`,
      "http://server.test:9000/recommendations/opportunities?kind=orphan",
      "http://server.test:9000/recommendations/summary",
    ]);
  });

  it("maps a plan 404 to a missing resource", async () => {
    globalFetch.mockResolvedValueOnce(jsonResponse(errorBody("not_found"), 404));
    const result = await serverApi.getLinkPlan(PLAN_ID);
    if (result.ok) throw new Error("unreachable");
    expect(serverApi.isMissingResourceError(result.error)).toBe(true);
  });

  it("returns a config error naming the path template when the env URL is invalid", async () => {
    vi.stubEnv("KEN_API_BASE_URL", "file:///etc/passwd");
    const result = await serverApi.listLinkPlans();
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toMatchObject({ kind: "config", path: "/recommendations" });
    expect(globalFetch).not.toHaveBeenCalled();
  });
});
