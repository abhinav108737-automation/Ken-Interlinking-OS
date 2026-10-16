// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from "vitest";

import { type ApiClient, type ApiResult, type FetchLike, createApiClient, isNotFoundError } from "@/lib/api-client";

import {
  analyticsImportView,
  analyticsSummary,
  linkPerformanceDetail,
  linkPerformanceView,
  pageImpactView,
} from "./fixtures/analytics";
import { OTHER_PAGE_ID, PAGE_ID, errorBody, jsonResponse, list } from "./fixtures/inventory";
import { PLAN_ID } from "./fixtures/recommendations";
import { TOKEN } from "./fixtures/review";

const BASE = "http://api.test:8000/prefix";

let fetchMock: ReturnType<typeof vi.fn<FetchLike>>;
let client: ApiClient;

beforeEach(() => {
  fetchMock = vi.fn<FetchLike>();
  client = createApiClient({ baseUrl: BASE, fetch: fetchMock, token: TOKEN });
});

function call(index = 0): { url: URL; init: RequestInit; headers: Record<string, string> } {
  const args = fetchMock.mock.calls[index];
  if (args === undefined) throw new Error("fetch was not called");
  return { url: new URL(String(args[0])), init: args[1], headers: args[1].headers as Record<string, string> };
}

function expectError(result: ApiResult<unknown>) {
  expect(result.ok).toBe(false);
  if (result.ok) throw new Error("unreachable");
  return result.error;
}

const CASES = [
  {
    name: "getAnalyticsSummary",
    run: (c: ApiClient) => c.getAnalyticsSummary(),
    path: "/prefix/analytics/summary",
    response: analyticsSummary(),
  },
  {
    name: "listLinkPerformance",
    run: (c: ApiClient) => c.listLinkPerformance(),
    path: "/prefix/analytics/links",
    response: list([linkPerformanceView()]),
  },
  {
    name: "getLinkPerformance",
    run: (c: ApiClient) => c.getLinkPerformance(PLAN_ID),
    path: `/prefix/analytics/links/${PLAN_ID}`,
    response: linkPerformanceDetail(),
  },
  {
    name: "getPageImpact",
    run: (c: ApiClient) => c.getPageImpact(OTHER_PAGE_ID),
    path: `/prefix/analytics/pages/${OTHER_PAGE_ID}`,
    response: pageImpactView(),
  },
  {
    name: "listAnalyticsImports",
    run: (c: ApiClient) => c.listAnalyticsImports(),
    path: "/prefix/analytics/imports",
    response: list([analyticsImportView()]),
  },
] as const;

describe.each(CASES)("$name", ({ run, path, response }) => {
  it("GETs the contract URL with the bearer token and parses the body", async () => {
    fetchMock.mockResolvedValue(jsonResponse(response, 200));
    const result = await run(client);
    expect(result).toMatchObject({ ok: true, data: response });
    const { url, init, headers } = call();
    expect(url.pathname).toBe(path);
    expect(init.method ?? "GET").toBe("GET");
    expect(init.body).toBeUndefined();
    expect(init.redirect).toBe("manual");
    expect(headers["Authorization"]).toBe(`Bearer ${TOKEN}`);
  });

  it("maps 401/403 to api_error and rejects an off-contract body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("unauthenticated"), 401));
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("permission_denied"), 403));
    fetchMock.mockResolvedValueOnce(jsonResponse({ unexpected: true }, 200));
    expect(expectError(await run(client))).toMatchObject({ kind: "api_error", status: 401 });
    expect(expectError(await run(client))).toMatchObject({ kind: "api_error", status: 403, code: "permission_denied" });
    expect(expectError(await run(client)).kind).toBe("invalid_body");
  });
});

describe("query encoding", () => {
  it("sends the summary range exactly as given", async () => {
    fetchMock.mockResolvedValue(jsonResponse(analyticsSummary(), 200));
    await client.getAnalyticsSummary({ start: "2026-09-01", end: "2026-09-28" });
    expect(call().url.search).toBe("?start=2026-09-01&end=2026-09-28");
  });

  it("sends every link filter, and omits null and undefined ones", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([linkPerformanceView()]), 200));
    await client.listLinkPerformance({
      start: "2026-09-01",
      end: "2026-09-28",
      source: "ga4",
      source_page_id: PAGE_ID,
      target_page_id: null,
      deployment_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      plan_status: "verified",
      sort: "clicks",
      order: "desc",
      limit: 10,
      offset: 0,
    });
    const { url } = call();
    expect(url.searchParams.get("source")).toBe("ga4");
    expect(url.searchParams.get("source_page_id")).toBe(PAGE_ID);
    expect(url.searchParams.has("target_page_id")).toBe(false);
    expect(url.searchParams.get("deployment_id")).toBe("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
    expect(url.searchParams.get("plan_status")).toBe("verified");
    expect(url.searchParams.get("sort")).toBe("clicks");
    expect(url.searchParams.get("order")).toBe("desc");
    expect(url.searchParams.get("limit")).toBe("10");
    // 0 is a real offset, not an absent value.
    expect(url.searchParams.get("offset")).toBe("0");
  });

  it("sends window_days and the per-link range", async () => {
    fetchMock.mockResolvedValue(jsonResponse(pageImpactView(), 200));
    await client.getPageImpact(OTHER_PAGE_ID, { window_days: 7 });
    expect(call().url.search).toBe("?window_days=7");
    fetchMock.mockResolvedValue(jsonResponse(linkPerformanceDetail(), 200));
    await client.getLinkPerformance(PLAN_ID, { start: "2026-09-01", end: "2026-09-28" });
    expect(call(1).url.search).toBe("?start=2026-09-01&end=2026-09-28");
  });

  it("sends no query at all when there is none", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([analyticsImportView()]), 200));
    await client.listAnalyticsImports();
    expect(call().url.search).toBe("");
  });
});

describe("declared errors", () => {
  it("declares 404 for the detail routes", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("not_found"), 404));
    const plan = expectError(await client.getLinkPerformance(PLAN_ID));
    expect(plan).toMatchObject({ kind: "api_error", status: 404, code: "not_found" });
    expect(isNotFoundError(plan)).toBe(true);
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("page_not_found"), 404));
    expect(isNotFoundError(expectError(await client.getPageImpact(OTHER_PAGE_ID)))).toBe(true);
  });

  it("declares 422 for the list routes", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("validation_error"), 422));
    expect(expectError(await client.listLinkPerformance({ limit: 1000 }))).toMatchObject({
      kind: "api_error",
      status: 422,
    });
  });

  it("refuses a malformed id without sending a request", async () => {
    expect(expectError(await client.getLinkPerformance("../secrets"))).toMatchObject({
      kind: "invalid_argument",
      path: "/analytics/links/{plan_id}",
    });
    expect(expectError(await client.getPageImpact("a/b"))).toMatchObject({
      kind: "invalid_argument",
      path: "/analytics/pages/{page_id}",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("response guards", () => {
  it("rejects a summary whose nested totals are the wrong shape", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ ...analyticsSummary(), totals_by_source: { ga4: { impressions: "lots", clicks: 1, ctr: null } } }, 200),
    );
    expect(expectError(await client.getAnalyticsSummary()).kind).toBe("invalid_body");
  });

  it("rejects a summary with a non-boolean synthetic flag", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ...analyticsSummary(), synthetic: "no" }, 200));
    expect(expectError(await client.getAnalyticsSummary()).kind).toBe("invalid_body");
  });

  it("rejects a link detail whose attribution or window statistics are incomplete", async () => {
    const detail = linkPerformanceDetail();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ...detail, attribution: { ...detail.attribution, policy_version: "three" } }, 200),
    );
    expect(expectError(await client.getLinkPerformance(PLAN_ID)).kind).toBe("invalid_body");
    const comparison = pageImpactView().changes[0]!;
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ...detail, target_impact: [{ ...comparison, pre: { ...comparison.pre, covered_days: null } }] }, 200),
    );
    expect(expectError(await client.getLinkPerformance(PLAN_ID)).kind).toBe("invalid_body");
  });

  it("accepts a never-deployed link: empty series, open-ended window, no measurement", async () => {
    const detail = linkPerformanceDetail({
      series: {},
      totals: {},
      live_window: { plan_id: PLAN_ID, deployed_at: null, rolled_back_at: null },
      measurement: null,
    });
    fetchMock.mockResolvedValue(jsonResponse(detail, 200));
    expect(await client.getLinkPerformance(PLAN_ID)).toMatchObject({ ok: true, data: detail });
  });

  it("accepts a body that omits the optional synthetic flags (an older server)", async () => {
    const withoutFlag: Record<string, unknown> = { ...linkPerformanceView() };
    delete withoutFlag["synthetic"];
    fetchMock.mockResolvedValue(jsonResponse(list([withoutFlag]), 200));
    const result = await client.listLinkPerformance();
    expect(result.ok).toBe(true);
  });

  it("rejects an import ledger row with the wrong counter type", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([{ ...analyticsImportView(), counters: { rows_read: "many" } }]), 200));
    expect(expectError(await client.listAnalyticsImports()).kind).toBe("invalid_body");
  });

  it("rejects a page impact view whose series holds a foreign point", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ...pageImpactView(), series: { ga4: [{ metric_date: "2026-09-01" }] } }, 200));
    expect(expectError(await client.getPageImpact(OTHER_PAGE_ID)).kind).toBe("invalid_body");
  });
});
