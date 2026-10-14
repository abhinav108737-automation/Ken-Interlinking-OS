// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as serverApi from "@/lib/api";
import {
  type ApiClient,
  type ApiResult,
  type FetchLike,
  createApiClient,
  isAgentPlanReview,
  isAgentPlanReviewList,
  isAgentRunDetail,
  isAgentRunSummary,
  isAgentStepView,
  isMissingResourceError,
  isNotFoundError,
  isToolCallRecord,
} from "@/lib/api-client";

import {
  AGENT_RUN_ID,
  agentPlanReview,
  agentRunDetail,
  agentRunSummary,
  agentStep,
  toolCall,
} from "./fixtures/agents";
import { PAGE_ID, errorBody, jsonResponse, list } from "./fixtures/inventory";
import { PLAN_ID } from "./fixtures/recommendations";

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
    name: "listAgentRuns",
    template: "/agents/runs",
    declared: [422],
    call: (c) => c.listAgentRuns(),
    body: list([agentRunSummary(), agentRunSummary({ outcome: null, triage_decision: null, finished_at: null })]),
    expectedPath: "/prefix/agents/runs",
  },
  {
    name: "getAgentRun",
    template: "/agents/runs/{run_id}",
    declared: [404, 422],
    call: (c) => c.getAgentRun(AGENT_RUN_ID),
    body: agentRunDetail(),
    expectedPath: `/prefix/agents/runs/${AGENT_RUN_ID}`,
  },
  {
    name: "listAgentPlanReviews",
    template: "/agents/plans/{plan_id}/reviews",
    declared: [422],
    call: (c) => c.listAgentPlanReviews(PLAN_ID),
    body: [agentPlanReview()],
    expectedPath: `/prefix/agents/plans/${PLAN_ID}/reviews`,
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

  it("maps a declared 422 to api_error", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("validation_failed"), 422));
    const error = expectError(await op.call(client));
    expect(error).toMatchObject({ kind: "api_error", path: op.template, code: "validation_failed" });
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

describe("agent query construction", () => {
  it("encodes filters and paging, and omits absent ones", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([]), 200));
    await client.listAgentRuns({ status: "failed", outcome: "failed", source_page_id: PAGE_ID, limit: 20, offset: 40 });
    await client.listAgentRuns({ status: null, outcome: null, source_page_id: null });
    expect(requestedUrl(0).search).toBe(`?status=failed&outcome=failed&source_page_id=${PAGE_ID}&limit=20&offset=40`);
    expect(requestedUrl(1).search).toBe("");
  });

  it("accepts an empty review list", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([], 200));
    expect(await client.listAgentPlanReviews(PLAN_ID)).toEqual({ ok: true, data: [], status: 200 });
  });

  it.each(["", ".", "..", "%2e%2e", "a/b", "a?b", "a#b", "a b", "x".repeat(65)])(
    "refuses run and plan id %j without sending a request",
    async (bad) => {
      const run = expectError(await client.getAgentRun(bad));
      expect(run).toMatchObject({ kind: "invalid_argument", path: "/agents/runs/{run_id}" });
      expect(isMissingResourceError(run)).toBe(true);
      const reviews = expectError(await client.listAgentPlanReviews(bad));
      expect(reviews).toMatchObject({ kind: "invalid_argument", path: "/agents/plans/{plan_id}/reviews" });
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );
});

describe("agent guards", () => {
  it("accept the fixtures, including nullable fields", () => {
    expect(isToolCallRecord(toolCall({ ok: false, error_code: "timeout" }))).toBe(true);
    expect(isAgentStepView(agentStep({ model_role: "fast", model: "m", fallback_reason: "model_disabled" }))).toBe(true);
    expect(isAgentPlanReview(agentPlanReview({ model: null, fallback_reason: "schema_violation" }))).toBe(true);
    expect(isAgentRunSummary(agentRunSummary({ source_page_version: null, error_code: "orphaned" }))).toBe(true);
    expect(isAgentRunDetail(agentRunDetail({ steps: [], reviews: [], review_notes: ["Look closely."] }))).toBe(true);
    expect(isAgentPlanReviewList([])).toBe(true);
  });

  it("reject malformed steps, tool calls, reviews and runs", () => {
    expect(isToolCallRecord({ ...toolCall(), ok: "yes" })).toBe(false);
    expect(isToolCallRecord({ ...toolCall(), summary: null })).toBe(false);
    expect(isAgentStepView({ ...agentStep(), tool_calls: [{ tool: "get_page" }] })).toBe(false);
    expect(isAgentStepView({ ...agentStep(), output: [] })).toBe(false);
    expect(isAgentStepView({ ...agentStep(), seq: "0" })).toBe(false);
    expect(isAgentPlanReview({ ...agentPlanReview(), reasons: "weak_relevance" })).toBe(false);
    expect(isAgentPlanReview({ ...agentPlanReview(), explanation: null })).toBe(false);
    expect(isAgentRunSummary({ ...agentRunSummary(), handed_off_plan_ids: null })).toBe(false);
    expect(isAgentRunDetail(agentRunSummary())).toBe(false);
    expect(isAgentRunDetail({ ...agentRunDetail(), config_snapshot: null })).toBe(false);
    expect(isAgentRunDetail({ ...agentRunDetail(), review_notes: [1] })).toBe(false);
    expect(isAgentPlanReviewList({ items: [] })).toBe(false);
  });
});

describe("agent server wrappers (lib/api.ts)", () => {
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
      .mockResolvedValueOnce(jsonResponse(list([agentRunSummary()]), 200))
      .mockResolvedValueOnce(jsonResponse(agentRunDetail(), 200))
      .mockResolvedValueOnce(jsonResponse([agentPlanReview()], 200));
    expect((await serverApi.listAgentRuns({ outcome: "recommend", limit: 10 })).ok).toBe(true);
    expect((await serverApi.getAgentRun(AGENT_RUN_ID)).ok).toBe(true);
    expect((await serverApi.listAgentPlanReviews(PLAN_ID)).ok).toBe(true);
    expect(globalFetch.mock.calls.map(([url]) => String(url))).toEqual([
      "http://server.test:9000/agents/runs?outcome=recommend&limit=10",
      `http://server.test:9000/agents/runs/${AGENT_RUN_ID}`,
      `http://server.test:9000/agents/plans/${PLAN_ID}/reviews`,
    ]);
  });

  it("maps a run 404 to a missing resource", async () => {
    globalFetch.mockResolvedValueOnce(jsonResponse(errorBody("not_found"), 404));
    const result = await serverApi.getAgentRun(AGENT_RUN_ID);
    if (result.ok) throw new Error("unreachable");
    expect(serverApi.isMissingResourceError(result.error)).toBe(true);
  });

  it("returns a config error naming the path template when the env URL is invalid", async () => {
    vi.stubEnv("KEN_API_BASE_URL", "file:///etc/passwd");
    const result = await serverApi.listAgentRuns();
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toMatchObject({ kind: "config", path: "/agents/runs" });
    expect(globalFetch).not.toHaveBeenCalled();
  });
});
