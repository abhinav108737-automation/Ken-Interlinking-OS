// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as serverApi from "@/lib/api";
import {
  type ApiClient,
  type ApiResult,
  type FetchLike,
  createApiClient,
  isIntelligenceCoverage,
  isJobRunDetail,
  isMissingResourceError,
  isModelUnavailableError,
  isNotFoundError,
  isPageIntelligenceView,
  isSafeNodeId,
  isTaxonomyView,
} from "@/lib/api-client";

import {
  JOB_RUN_ID,
  coverage,
  jobRunDetail,
  jobRunSummary,
  pageIntelligence,
  relationship,
  similarPage,
  taxonomy,
} from "./fixtures/intelligence";
import { PAGE_ID, errorBody, jsonResponse, list } from "./fixtures/inventory";

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

/** Every new operation: a valid 200 body, its path template and which errors the contract declares. */
const OPERATIONS: ReadonlyArray<{
  name: string;
  template: string;
  declared: readonly number[];
  call: (c: ApiClient) => Promise<ApiResult<unknown>>;
  body: unknown;
  expectedPath: string;
  expectedSearch?: string;
}> = [
  {
    name: "getIntelligenceCoverage",
    template: "/intelligence/coverage",
    declared: [],
    call: (c) => c.getIntelligenceCoverage(),
    body: coverage(),
    expectedPath: "/prefix/intelligence/coverage",
  },
  {
    name: "getTaxonomy",
    template: "/intelligence/taxonomy",
    declared: [],
    call: (c) => c.getTaxonomy(),
    body: taxonomy(),
    expectedPath: "/prefix/intelligence/taxonomy",
  },
  {
    name: "listTaxonomyNodePages",
    template: "/intelligence/taxonomy/{node_id}/pages",
    declared: [404, 422],
    call: (c) => c.listTaxonomyNodePages("energy.batteries"),
    body: list([similarPage()]),
    expectedPath: "/prefix/intelligence/taxonomy/energy.batteries/pages",
  },
  {
    name: "getPageIntelligence",
    template: "/intelligence/pages/{page_id}",
    declared: [404, 422],
    call: (c) => c.getPageIntelligence(PAGE_ID),
    body: pageIntelligence(),
    expectedPath: `/prefix/intelligence/pages/${PAGE_ID}`,
  },
  {
    name: "listSimilarPages",
    template: "/intelligence/pages/{page_id}/similar",
    declared: [404, 422],
    call: (c) => c.listSimilarPages(PAGE_ID),
    body: list([similarPage()]),
    expectedPath: `/prefix/intelligence/pages/${PAGE_ID}/similar`,
  },
  {
    name: "listPageRelationships",
    template: "/intelligence/pages/{page_id}/relationships",
    declared: [404, 422],
    call: (c) => c.listPageRelationships(PAGE_ID),
    body: list([relationship()]),
    expectedPath: `/prefix/intelligence/pages/${PAGE_ID}/relationships`,
  },
  {
    name: "semanticSearch",
    template: "/intelligence/search",
    declared: [422, 503],
    call: (c) => c.semanticSearch({ q: "ev batteries" }),
    body: list([similarPage()]),
    expectedPath: "/prefix/intelligence/search",
    expectedSearch: "?q=ev+batteries",
  },
  {
    name: "listJobRuns",
    template: "/jobs/runs",
    declared: [422],
    call: (c) => c.listJobRuns(),
    body: list([jobRunSummary()]),
    expectedPath: "/prefix/jobs/runs",
  },
  {
    name: "getJobRun",
    template: "/jobs/runs/{run_id}",
    declared: [404, 422],
    call: (c) => c.getJobRun(JOB_RUN_ID),
    body: jobRunDetail(),
    expectedPath: `/prefix/jobs/runs/${JOB_RUN_ID}`,
  },
];

describe.each(OPERATIONS)("$name", (op) => {
  it("GETs the right URL and parses a 200", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(op.body, 200));
    expect(await op.call(client)).toEqual({ ok: true, data: op.body, status: 200 });
    const url = requestedUrl();
    expect(url.origin).toBe("http://api.test:8000");
    expect(url.pathname).toBe(op.expectedPath);
    expect(url.search).toBe(op.expectedSearch ?? "");
    const init = fetchMock.mock.calls[0]![1];
    expect(init.method).toBe("GET");
    expect(init.cache).toBe("no-store");
  });

  it(op.declared.includes(422) ? "maps a declared 422 to api_error" : "treats an undeclared 422 as http_status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("validation_failed"), 422));
    const error = expectError(await op.call(client));
    if (op.declared.includes(422)) {
      expect(error).toMatchObject({ kind: "api_error", status: 422, code: "validation_failed", path: op.template, requestId: "req-1" });
      expect(isMissingResourceError(error)).toBe(true);
    } else {
      expect(error).toMatchObject({ kind: "http_status", status: 422, code: "validation_failed", path: op.template });
      expect(isMissingResourceError(error)).toBe(false);
    }
  });

  it(op.declared.includes(404) ? "maps a declared 404 to not-found" : "treats an undeclared 404 as http_status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("not_found", null), 404));
    const error = expectError(await op.call(client));
    expect(isNotFoundError(error)).toBe(op.declared.includes(404));
    expect(error.kind).toBe(op.declared.includes(404) ? "api_error" : "http_status");
  });

  it(op.declared.includes(503) ? "maps a declared 503 to model_unavailable" : "treats a 503 as http_status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("model_unavailable"), 503));
    const error = expectError(await op.call(client));
    expect(error.status).toBe(503);
    expect(error.code).toBe("model_unavailable");
    expect(isModelUnavailableError(error)).toBe(op.declared.includes(503));
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

  it("returns a config error for an invalid base URL without calling fetch", async () => {
    const bad = createApiClient({ baseUrl: "javascript:alert(1)", fetch: fetchMock });
    expect(expectError(await op.call(bad)).kind).toBe("config");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("semantic search 503", () => {
  it("is an api_error with code model_unavailable and an operator-safe message", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("model_unavailable"), 503));
    const error = expectError(await client.semanticSearch({ q: "batteries" }));
    expect(error).toMatchObject({
      kind: "api_error",
      status: 503,
      code: "model_unavailable",
      path: "/intelligence/search",
      requestId: "req-1",
    });
    expect(error.message).toBe("The API reported that a service needed for /intelligence/search is unavailable (HTTP 503).");
    expect(isModelUnavailableError(error)).toBe(true);
    expect(isMissingResourceError(error)).toBe(false);
  });

  it("is not model_unavailable when the 503 carries another code or no ErrorResponse", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("service_unavailable"), 503));
    expect(isModelUnavailableError(expectError(await client.semanticSearch({ q: "batteries" })))).toBe(false);
    fetchMock.mockResolvedValueOnce(new Response("<html>down</html>", { status: 503 }));
    expect(expectError(await client.semanticSearch({ q: "batteries" }))).toMatchObject({ kind: "invalid_body", status: 503 });
  });
});

describe("query construction", () => {
  it("encodes each operation's parameters and omits absent ones", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([]), 200));
    await client.semanticSearch({ q: "ev & batteries/ü?#", limit: 20 });
    await client.listPageRelationships(PAGE_ID, { relationship_type: "submarket", limit: 25, offset: 50 });
    await client.listSimilarPages(PAGE_ID, { limit: 5 });
    await client.listTaxonomyNodePages("in", { offset: 100 });
    await client.listJobRuns({ kind: "analysis", status: "failed", limit: 10 });
    await client.listJobRuns({ kind: null, status: null });
    expect(requestedUrl(0).searchParams.get("q")).toBe("ev & batteries/ü?#");
    expect(requestedUrl(0).search).toBe("?q=ev+%26+batteries%2F%C3%BC%3F%23&limit=20");
    expect(requestedUrl(1).search).toBe("?relationship_type=submarket&limit=25&offset=50");
    expect(requestedUrl(2).search).toBe("?limit=5");
    expect(requestedUrl(3).search).toBe("?offset=100");
    expect(requestedUrl(4).search).toBe("?kind=analysis&status=failed&limit=10");
    expect(requestedUrl(5).search).toBe("");
  });
});

describe("path identifiers", () => {
  it.each(["", ".", "..", "%2e%2e", "a/b", "a?b", "a#b", "a b", "x".repeat(65)])(
    "refuses page/run id %j without sending a request",
    async (bad) => {
      const results = await Promise.all([
        client.getPageIntelligence(bad),
        client.listSimilarPages(bad),
        client.listPageRelationships(bad),
        client.getJobRun(bad),
      ]);
      for (const result of results) expect(expectError(result).kind).toBe("invalid_argument");
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it.each(["", ".", "..", ".energy", "Energy", "a/b", "a%2Fb", "a?b", "a#b", "a b", "e".repeat(101)])(
    "refuses taxonomy node id %j",
    async (bad) => {
      const error = expectError(await client.listTaxonomyNodePages(bad));
      expect(error).toMatchObject({ kind: "invalid_argument", path: "/intelligence/taxonomy/{node_id}/pages" });
      expect(isMissingResourceError(error)).toBe(true);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("accepts contract-shaped node ids as one path segment", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([]), 200));
    expect(isSafeNodeId("energy.batteries_ev-2")).toBe(true);
    expect(isSafeNodeId("a..")).toBe(true);
    expect(isSafeNodeId(7)).toBe(false);
    await client.listTaxonomyNodePages("a..");
    expect(requestedUrl().pathname).toBe("/prefix/intelligence/taxonomy/a../pages");
  });
});

describe("intelligence guards", () => {
  it("accepts a page with no analysis and no embedding", () => {
    expect(isPageIntelligenceView(pageIntelligence({ analysis: null, embedding: null, analysis_fresh: false }))).toBe(true);
  });

  it("rejects malformed nested intelligence", () => {
    const base = pageIntelligence();
    expect(isPageIntelligenceView({ ...base, eligibility: { ...base.eligibility, source_reasons: "gone" } })).toBe(false);
    expect(isPageIntelligenceView({ ...base, protections: [{ id: "1" }] })).toBe(false);
    expect(isPageIntelligenceView({ ...base, analysis: { ...base.analysis, topics: [{ node_id: 1 }] } })).toBe(false);
    expect(isPageIntelligenceView({ ...base, analysis: { ...base.analysis, keywords: [1] } })).toBe(false);
    expect(isPageIntelligenceView({ ...base, embedding: { ...base.embedding, dimensions: "768" } })).toBe(false);
    expect(isPageIntelligenceView({ ...base, relationship_counts: { same_market: "2" } })).toBe(false);
  });

  it("rejects coverage, taxonomy and job runs missing fields", () => {
    const noIndustry: Record<string, unknown> = { ...coverage() };
    delete noIndustry["by_industry"];
    expect(isIntelligenceCoverage(coverage())).toBe(true);
    expect(isIntelligenceCoverage(noIndustry)).toBe(false);
    expect(isTaxonomyView(taxonomy())).toBe(true);
    expect(isTaxonomyView({ ...taxonomy(), markets: [{ id: "x", label: "X" }] })).toBe(false);
    expect(isJobRunDetail(jobRunDetail())).toBe(true);
    expect(isJobRunDetail({ ...jobRunDetail(), config_snapshot: null })).toBe(false);
    expect(isJobRunDetail({ ...jobRunDetail(), counters: { a: "1" } })).toBe(false);
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
      .mockResolvedValueOnce(jsonResponse(coverage(), 200))
      .mockResolvedValueOnce(jsonResponse(taxonomy(), 200))
      .mockResolvedValueOnce(jsonResponse(pageIntelligence(), 200))
      .mockImplementation(async () => jsonResponse(list([]), 200));
    expect((await serverApi.getIntelligenceCoverage()).ok).toBe(true);
    expect((await serverApi.getTaxonomy()).ok).toBe(true);
    expect((await serverApi.getPageIntelligence(PAGE_ID)).ok).toBe(true);
    await serverApi.listSimilarPages(PAGE_ID, { limit: 10 });
    await serverApi.listPageRelationships(PAGE_ID, { offset: 50 });
    await serverApi.listTaxonomyNodePages("energy", { limit: 20 });
    await serverApi.semanticSearch({ q: "lithium", limit: 5 });
    await serverApi.listJobRuns({ kind: "analysis" });
    expect(globalFetch.mock.calls.map(([url]) => String(url))).toEqual([
      "http://server.test:9000/intelligence/coverage",
      "http://server.test:9000/intelligence/taxonomy",
      `http://server.test:9000/intelligence/pages/${PAGE_ID}`,
      `http://server.test:9000/intelligence/pages/${PAGE_ID}/similar?limit=10`,
      `http://server.test:9000/intelligence/pages/${PAGE_ID}/relationships?offset=50`,
      "http://server.test:9000/intelligence/taxonomy/energy/pages?limit=20",
      "http://server.test:9000/intelligence/search?q=lithium&limit=5",
      "http://server.test:9000/jobs/runs?kind=analysis",
    ]);
  });

  it("maps a job run 404 and a search 503", async () => {
    globalFetch
      .mockResolvedValueOnce(jsonResponse(errorBody("not_found"), 404))
      .mockResolvedValueOnce(jsonResponse(errorBody("model_unavailable"), 503));
    const run = await serverApi.getJobRun(JOB_RUN_ID);
    const search = await serverApi.semanticSearch({ q: "lithium" });
    if (run.ok || search.ok) throw new Error("unreachable");
    expect(serverApi.isMissingResourceError(run.error)).toBe(true);
    expect(serverApi.isModelUnavailableError(search.error)).toBe(true);
  });

  it("returns a config error naming the path template when the env URL is invalid", async () => {
    vi.stubEnv("KEN_API_BASE_URL", "file:///etc/passwd");
    const result = await serverApi.semanticSearch({ q: "lithium" });
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toMatchObject({ kind: "config", path: "/intelligence/search" });
    expect(globalFetch).not.toHaveBeenCalled();
  });
});
