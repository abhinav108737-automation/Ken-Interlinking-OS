// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as serverApi from "@/lib/api";
import {
  type ApiClient,
  type ApiResult,
  type FetchLike,
  buildQuery,
  createApiClient,
  isListResponse,
  isNotFoundError,
  isPageSummary,
  isSafePathId,
} from "@/lib/api-client";

import {
  PAGE_ID,
  RUN_ID,
  errorBody,
  issue,
  jsonResponse,
  link,
  list,
  pageDetail,
  pageSummary,
  runDetail,
  runSummary,
  versionDetail,
  versionSummary,
} from "./fixtures/inventory";

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

/** Every operation, with a valid 200 body and the path template it reports. */
const OPERATIONS: ReadonlyArray<{
  name: string;
  template: string;
  declares404: boolean;
  call: (c: ApiClient) => Promise<ApiResult<unknown>>;
  body: unknown;
  expectedPath: string;
}> = [
  {
    name: "listInventoryPages",
    template: "/inventory/pages",
    declares404: false,
    call: (c) => c.listInventoryPages(),
    body: list([pageSummary()]),
    expectedPath: "/prefix/inventory/pages",
  },
  {
    name: "getInventoryPage",
    template: "/inventory/pages/{page_id}",
    declares404: true,
    call: (c) => c.getInventoryPage(PAGE_ID),
    body: pageDetail(),
    expectedPath: `/prefix/inventory/pages/${PAGE_ID}`,
  },
  {
    name: "listInventoryPageVersions",
    template: "/inventory/pages/{page_id}/versions",
    declares404: true,
    call: (c) => c.listInventoryPageVersions(PAGE_ID),
    body: list([versionSummary()]),
    expectedPath: `/prefix/inventory/pages/${PAGE_ID}/versions`,
  },
  {
    name: "getInventoryPageVersion",
    template: "/inventory/pages/{page_id}/versions/{version}",
    declares404: true,
    call: (c) => c.getInventoryPageVersion(PAGE_ID, 2),
    body: versionDetail(),
    expectedPath: `/prefix/inventory/pages/${PAGE_ID}/versions/2`,
  },
  {
    name: "listInventoryPageLinks",
    template: "/inventory/pages/{page_id}/links",
    declares404: true,
    call: (c) => c.listInventoryPageLinks(PAGE_ID),
    body: list([link()]),
    expectedPath: `/prefix/inventory/pages/${PAGE_ID}/links`,
  },
  {
    name: "listInventoryLinks",
    template: "/inventory/links",
    declares404: false,
    call: (c) => c.listInventoryLinks(),
    body: list([link()]),
    expectedPath: "/prefix/inventory/links",
  },
  {
    name: "listIngestionRuns",
    template: "/inventory/runs",
    declares404: false,
    call: (c) => c.listIngestionRuns(),
    body: list([runSummary()]),
    expectedPath: "/prefix/inventory/runs",
  },
  {
    name: "getIngestionRun",
    template: "/inventory/runs/{run_id}",
    declares404: true,
    call: (c) => c.getIngestionRun(RUN_ID),
    body: runDetail(),
    expectedPath: `/prefix/inventory/runs/${RUN_ID}`,
  },
  {
    name: "listIngestionRunIssues",
    template: "/inventory/runs/{run_id}/issues",
    declares404: true,
    call: (c) => c.listIngestionRunIssues(RUN_ID),
    body: list([issue()]),
    expectedPath: `/prefix/inventory/runs/${RUN_ID}/issues`,
  },
];

describe.each(OPERATIONS)("$name", (op) => {
  it("GETs the right URL with no query string by default and parses a 200", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(op.body, 200));
    const result = await op.call(client);
    expect(result).toEqual({ ok: true, data: op.body, status: 200 });
    const url = requestedUrl();
    expect(url.origin).toBe("http://api.test:8000");
    expect(url.pathname).toBe(op.expectedPath);
    expect(url.search).toBe("");
    const init = fetchMock.mock.calls[0]![1];
    expect(init.method).toBe("GET");
    expect(init.cache).toBe("no-store");
  });

  it("maps a 422 ErrorResponse to an api_error carrying the code", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("validation_failed"), 422));
    const error = expectError(await op.call(client));
    expect(error).toMatchObject({
      kind: "api_error",
      status: 422,
      code: "validation_failed",
      path: op.template,
      requestId: "req-1",
    });
    expect(isNotFoundError(error)).toBe(false);
  });

  it(op.declares404 ? "maps a 404 ErrorResponse to a not-found api_error" : "treats an undeclared 404 as http_status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody(op.declares404 ? "page_not_found" : "not_found", null), 404));
    const error = expectError(await op.call(client));
    if (op.declares404) {
      expect(error).toMatchObject({ kind: "api_error", status: 404, code: "page_not_found" });
      expect(error).not.toHaveProperty("requestId");
      expect(isNotFoundError(error)).toBe(true);
    } else {
      expect(error).toMatchObject({ kind: "http_status", status: 404, code: "not_found" });
      expect(isNotFoundError(error)).toBe(false);
    }
  });

  it("treats a 200 body that misses the contract shape as invalid_body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: "nope", page: {} }, 200));
    expect(expectError(await op.call(client))).toMatchObject({ kind: "invalid_body", status: 200 });
  });

  it("treats a declared error status with a non-ErrorResponse body as invalid_body", async () => {
    fetchMock.mockResolvedValueOnce(new Response("<html>bad gateway</html>", { status: 422 }));
    expect(expectError(await op.call(client))).toMatchObject({ kind: "invalid_body", status: 422 });
  });

  it("maps an undeclared 500 to http_status, keeping the code", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("internal_error"), 500));
    expect(expectError(await op.call(client))).toMatchObject({
      kind: "http_status",
      status: 500,
      code: "internal_error",
    });
  });

  it("reports network failure and timeouts without throwing or echoing the base URL", async () => {
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

describe("query construction", () => {
  it("encodes list filters with URLSearchParams and omits null/undefined/empty", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([]), 200));
    await client.listInventoryPages({
      q: "widgets & gadgets/ü?#",
      publication_state: "draft",
      limit: 25,
      offset: 50,
    });
    const url = requestedUrl();
    expect(url.searchParams.get("q")).toBe("widgets & gadgets/ü?#");
    expect(url.search).toContain("q=widgets+%26+gadgets%2F%C3%BC%3F%23");
    expect(url.searchParams.get("publication_state")).toBe("draft");
    expect(url.searchParams.get("limit")).toBe("25");
    expect(url.searchParams.get("offset")).toBe("50");
    expect(url.hash).toBe("");

    await client.listInventoryPages({ q: null, publication_state: null });
    expect(requestedUrl(1).search).toBe("");
  });

  it("sends each operation's own filters", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([]), 200));
    await client.listInventoryLinks({ target_page_id: PAGE_ID, link_type: "internal", resolution: "broken" });
    await client.listInventoryPageLinks(PAGE_ID, { version: 3, link_type: "external", offset: 0 });
    await client.listIngestionRuns({ source_type: "file_export", status: "failed" });
    await client.listIngestionRunIssues(RUN_ID, { severity: "warning", limit: 10 });
    await client.listInventoryPageVersions(PAGE_ID, { offset: 100 });
    expect(requestedUrl(0).search).toBe(`?target_page_id=${PAGE_ID}&link_type=internal&resolution=broken`);
    expect(requestedUrl(1).search).toBe("?version=3&link_type=external&offset=0");
    expect(requestedUrl(2).search).toBe("?source_type=file_export&status=failed");
    expect(requestedUrl(3).search).toBe("?severity=warning&limit=10");
    expect(requestedUrl(4).search).toBe("?offset=100");
  });

  it("buildQuery skips absent values and stringifies the rest", () => {
    expect(buildQuery(undefined).toString()).toBe("");
    expect(buildQuery({ a: "", b: null, c: undefined, d: 0, e: false, f: "x y" }).toString()).toBe(
      "d=0&e=false&f=x+y",
    );
  });
});

describe("path identifiers", () => {
  it.each([
    "",
    ".",
    "..",
    "%2e%2e",
    "../runs",
    "a/b",
    "a?b",
    "a#b",
    "a b",
    "x".repeat(65),
  ])("refuses %j without sending a request", async (bad) => {
    const results = await Promise.all([
      client.getInventoryPage(bad),
      client.listInventoryPageVersions(bad),
      client.getInventoryPageVersion(bad, 1),
      client.listInventoryPageLinks(bad),
      client.getIngestionRun(bad),
      client.listIngestionRunIssues(bad),
    ]);
    for (const result of results) {
      expect(expectError(result).kind).toBe("invalid_argument");
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY])("refuses version %s", async (version) => {
    const error = expectError(await client.getInventoryPageVersion(PAGE_ID, version));
    expect(error.kind).toBe("invalid_argument");
    expect(error.path).toBe("/inventory/pages/{page_id}/versions/{version}");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("accepts uuid-like ids and keeps them as a single path segment", async () => {
    fetchMock.mockResolvedValue(jsonResponse(runDetail(), 200));
    await client.getIngestionRun("ABCdef-0123");
    expect(requestedUrl().pathname).toBe("/prefix/inventory/runs/ABCdef-0123");
    expect(isSafePathId(PAGE_ID)).toBe(true);
    expect(isSafePathId(42)).toBe(false);
  });
});

describe("inventory guards", () => {
  it("rejects a list whose items miss key fields or whose page info is incomplete", () => {
    expect(isListResponse(list([pageSummary()]), isPageSummary)).toBe(true);
    const noId: Record<string, unknown> = { ...pageSummary() };
    delete noId["id"];
    expect(isListResponse({ items: [noId], page: list([]).page }, isPageSummary)).toBe(false);
    expect(isListResponse({ items: [], page: { total: 0, limit: 50, offset: 0 } }, isPageSummary)).toBe(false);
    expect(isListResponse({ items: [], page: { total: 0, limit: 50, offset: 0, has_more: "no" } }, isPageSummary)).toBe(false);
  });

  it("rejects a page detail whose current version lacks body text or headings", async () => {
    const broken = pageDetail({ current: { ...versionDetail(), headings: "h1" as never } });
    fetchMock.mockResolvedValueOnce(jsonResponse(broken, 200));
    expect(expectError(await client.getInventoryPage(PAGE_ID)).kind).toBe("invalid_body");

    const noText = { ...pageDetail(), current: { ...versionDetail(), body_text: null } };
    fetchMock.mockResolvedValueOnce(jsonResponse(noText, 200));
    expect(expectError(await client.getInventoryPage(PAGE_ID)).kind).toBe("invalid_body");
  });

  it("rejects runs with a missing counter or non-numeric issue counts", async () => {
    const partial: Record<string, unknown> = { ...runSummary().counters };
    delete partial["pages_gone"];
    fetchMock.mockResolvedValueOnce(jsonResponse(list([{ ...runSummary(), counters: partial }]), 200));
    expect(expectError(await client.listIngestionRuns()).kind).toBe("invalid_body");

    fetchMock.mockResolvedValueOnce(jsonResponse(runDetail({ issue_counts: { x: "1" as never } }), 200));
    expect(expectError(await client.getIngestionRun(RUN_ID)).kind).toBe("invalid_body");
  });

  it("rejects a link whose rel is not a string array", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(list([{ ...link(), rel: "nofollow" }]), 200));
    expect(expectError(await client.listInventoryLinks()).kind).toBe("invalid_body");
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

  it("resolves KEN_API_BASE_URL per call and forwards path ids and queries", async () => {
    globalFetch.mockImplementation(async () => jsonResponse(list([]), 200));
    await serverApi.listInventoryPages({ q: "x" });
    await serverApi.listInventoryPageVersions(PAGE_ID, { offset: 50 });
    await serverApi.listInventoryPageLinks(PAGE_ID, { version: 1 });
    await serverApi.listInventoryLinks({ target_page_id: PAGE_ID });
    await serverApi.listIngestionRuns({ status: "running" });
    await serverApi.listIngestionRunIssues(RUN_ID, { severity: "error" });
    const urls = globalFetch.mock.calls.map(([url]) => String(url));
    expect(urls).toEqual([
      "http://server.test:9000/inventory/pages?q=x",
      `http://server.test:9000/inventory/pages/${PAGE_ID}/versions?offset=50`,
      `http://server.test:9000/inventory/pages/${PAGE_ID}/links?version=1`,
      `http://server.test:9000/inventory/links?target_page_id=${PAGE_ID}`,
      "http://server.test:9000/inventory/runs?status=running",
      `http://server.test:9000/inventory/runs/${RUN_ID}/issues?severity=error`,
    ]);
  });

  it("parses single resources and maps 404", async () => {
    globalFetch
      .mockResolvedValueOnce(jsonResponse(pageDetail(), 200))
      .mockResolvedValueOnce(jsonResponse(versionDetail(), 200))
      .mockResolvedValueOnce(jsonResponse(errorBody("not_found"), 404));
    expect((await serverApi.getInventoryPage(PAGE_ID)).ok).toBe(true);
    expect((await serverApi.getInventoryPageVersion(PAGE_ID, 2)).ok).toBe(true);
    const run = await serverApi.getIngestionRun(RUN_ID);
    expect(run.ok).toBe(false);
    if (run.ok) throw new Error("unreachable");
    expect(serverApi.isNotFoundError(run.error)).toBe(true);
  });

  it("returns a config error naming the path template when the env URL is invalid", async () => {
    vi.stubEnv("KEN_API_BASE_URL", "file:///etc/passwd");
    const result = await serverApi.getInventoryPage(PAGE_ID);
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toMatchObject({ kind: "config", path: "/inventory/pages/{page_id}" });
    expect(globalFetch).not.toHaveBeenCalled();
  });
});
