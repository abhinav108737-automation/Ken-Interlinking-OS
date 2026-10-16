import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AnalyticsPage from "@/app/analytics/page";
import PageImpactPage from "@/app/analytics/pages/[pageId]/page";
import DeploymentDetailPage from "@/app/deployments/[deploymentId]/page";
import RecommendationDetailPage from "@/app/recommendations/[planId]/page";
import type { ApiError, ApiResult, Role } from "@/lib/api-client";

import {
  analyticsImportView,
  analyticsSummary,
  dailySeries,
  linkDailyPoints,
  linkPerformanceDetail,
  linkPerformanceView,
  metricTotals,
  pageDailyPoints,
  pageImpactView,
  pageWindowComparison,
  sourceFreshness,
  windowStats,
} from "./fixtures/analytics";
import { DEPLOYMENT_ID, deploymentDetail } from "./fixtures/deployments";
import { OTHER_PAGE_ID, list } from "./fixtures/inventory";
import { PLAN_ID, linkPlanDetail, recommendationSummary, transition } from "./fixtures/recommendations";
import { principal } from "./fixtures/review";

const api = vi.hoisted(() => ({
  getAnalyticsSummary: vi.fn(),
  listAnalyticsImports: vi.fn(),
  listLinkPerformance: vi.fn(),
  getLinkPerformance: vi.fn(),
  getPageImpact: vi.fn(),
  // The screens that host the analytics panels.
  getDeployment: vi.fn(),
  getLinkPlan: vi.fn(),
  listPlanDeployments: vi.fn(),
  listAgentPlanReviews: vi.fn(),
  listPlanReviewActions: vi.fn(),
  listPlanTargetOptions: vi.fn(),
  getRecommendationSummary: vi.fn(),
  requireSessionUser: vi.fn(),
}));
const nav = vi.hoisted(() => ({
  notFound: vi.fn(() => {
    throw new Error("NEXT_NOT_FOUND");
  }),
}));

vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/api")>()), ...api }));
vi.mock("next/navigation", async (importOriginal) => ({
  ...(await importOriginal<typeof import("next/navigation")>()),
  notFound: nav.notFound,
  usePathname: () => "/analytics",
}));

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, status: 200 };
}

function fail(error: Partial<ApiError>): ApiResult<never> {
  return { ok: false, error: { kind: "api_error", path: "/analytics/summary", message: "It failed.", ...error } };
}

function as(role: Role) {
  api.requireSessionUser.mockResolvedValue(ok(principal(role)));
}

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  nav.notFound.mockClear();
  as("operator_admin");
  api.getAnalyticsSummary.mockResolvedValue(ok(analyticsSummary()));
  api.listAnalyticsImports.mockResolvedValue(ok(list([analyticsImportView()])));
  api.listLinkPerformance.mockResolvedValue(ok(list([linkPerformanceView()])));
  api.getLinkPerformance.mockResolvedValue(ok(linkPerformanceDetail()));
  api.getPageImpact.mockResolvedValue(ok(pageImpactView()));
  api.listPlanDeployments.mockResolvedValue(ok([]));
  api.listAgentPlanReviews.mockResolvedValue(ok([]));
  api.listPlanReviewActions.mockResolvedValue(ok([]));
  api.listPlanTargetOptions.mockResolvedValue(ok([]));
  api.getRecommendationSummary.mockResolvedValue(ok(recommendationSummary()));
});

const XSS = '<img src=x onerror="alert(1)">';

async function renderOverview(search: Record<string, string> = {}) {
  return render(await AnalyticsPage({ searchParams: Promise.resolve(search) }));
}

describe("/analytics", () => {
  it("asks for the API's own range, lists the KPIs, the daily series, top links, freshness and imports", async () => {
    await renderOverview();
    expect(api.getAnalyticsSummary).toHaveBeenCalledWith({});
    expect(api.listAnalyticsImports).toHaveBeenCalledWith({ limit: 10 });
    // No source chosen: the summary's own top links are used, so no second query.
    expect(api.listLinkPerformance).not.toHaveBeenCalled();

    const kpis = screen.getByLabelText("Key figures");
    expect(kpis).toHaveTextContent("Live links");
    expect(kpis).toHaveTextContent("7");
    expect(kpis).toHaveTextContent("1,000");
    expect(kpis).toHaveTextContent("2.50%");
    expect(kpis).not.toHaveTextContent(/landing/i);
    expect(screen.getByText(/Range shown: 2026-09-01 to 2026-09-28 \(28 days\)/)).toBeInTheDocument();

    expect(screen.getByRole("img", { name: /Impressions per day: 3 days/ })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /daily values/ })).toHaveTextContent("2026-09-03");
    expect(screen.getByRole("table", { name: /Most clicked links \(all sources\)/ })).toHaveTextContent("First-party");
    expect(screen.getByRole("table", { name: /Source freshness/ })).toHaveTextContent("2026-09-28");
    expect(screen.getByRole("table", { name: /Recent imports/ })).toHaveTextContent("812");
    expect(screen.getByText(/A failed import leaves no ledger row/)).toBeInTheDocument();
  });

  it("is a read-only screen: a GET filter form and no mutation control", async () => {
    const { container } = await renderOverview();
    const forms = container.querySelectorAll("form");
    expect(forms).toHaveLength(1);
    expect(forms[0]).toHaveAttribute("method", "get");
    expect(screen.queryByRole("button", { name: /import|deploy|approve|reject|refresh/i })).toBeNull();
    // Imports are CLI-only, so the screen says so instead of offering a trigger.
    expect(screen.getByText(/never calls an analytics provider/)).toBeInTheDocument();
  });

  it.each(["viewer", "reviewer", "operator_admin"] as const)("%s sees the same read-only screen", async (role) => {
    as(role);
    await renderOverview();
    expect(screen.getByRole("heading", { level: 1, name: "Analytics" })).toBeInTheDocument();
    expect(screen.getByLabelText("Key figures")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /deploy|approve|reject/i })).toBeNull();
    // Nothing on this screen depends on a role: the API enforces `read`.
    expect(api.requireSessionUser).not.toHaveBeenCalled();
  });

  it("sends a valid range and narrows the top links to a chosen source", async () => {
    api.listLinkPerformance.mockResolvedValue(
      ok(list([linkPerformanceView({ totals: { ga4: metricTotals({ impressions: 10, clicks: 1, ctr: 0.1 }) } })])),
    );
    api.getAnalyticsSummary.mockResolvedValue(
      ok(
        analyticsSummary({
          totals_by_source: {
            ga4: metricTotals({ impressions: 10, clicks: 1, ctr: 0.1 }),
            first_party: metricTotals({ impressions: 990, clicks: 24, ctr: 0.0242 }),
          },
          daily: [...dailySeries(2, { source: "ga4", impressions: 5, clicks: 1 }), ...dailySeries(2)],
        }),
      ),
    );
    await renderOverview({ start: "2026-09-01", end: "2026-09-28", source: "ga4" });
    expect(api.getAnalyticsSummary).toHaveBeenCalledWith({ start: "2026-09-01", end: "2026-09-28" });
    expect(api.listLinkPerformance).toHaveBeenCalledWith({
      start: "2026-09-01",
      end: "2026-09-28",
      source: "ga4",
      sort: "clicks",
      order: "desc",
      limit: 10,
    });
    expect(screen.getByLabelText("Source")).toHaveValue("ga4");
    expect(screen.getByLabelText("Start date")).toHaveValue("2026-09-01");
    // The tiles and the chart follow the chosen source only.
    const kpis = screen.getByLabelText("Key figures");
    expect(kpis).toHaveTextContent("10");
    expect(kpis).toHaveTextContent("10.00%");
    expect(screen.getByRole("table", { name: /Most clicked links \(GA4\)/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Impressions per day: 2 days/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Clear filters" })).toHaveAttribute("href", "/analytics");
  });

  it("warns about summing sources only when several hold data", async () => {
    api.getAnalyticsSummary.mockResolvedValue(
      ok(analyticsSummary({ totals_by_source: { ga4: metricTotals(), first_party: metricTotals() } })),
    );
    const many = await renderOverview();
    expect(screen.getByText(/not a count of unique impressions or clicks/)).toBeInTheDocument();
    many.unmount();
    api.getAnalyticsSummary.mockResolvedValue(ok(analyticsSummary({ totals_by_source: { first_party: metricTotals() } })));
    await renderOverview();
    expect(screen.queryByText(/not a count of unique impressions or clicks/)).toBeNull();
  });

  it.each([
    [{ start: "2026-09-01" }, "Give both a start and an end date, or neither."],
    [{ start: "2026-02-30", end: "2026-03-05" }, "Dates must be real calendar days in YYYY-MM-DD form."],
    [{ start: "2026-09-28", end: "2026-09-01" }, "The end date must not be before the start date."],
    [{ start: "2025-01-01", end: "2026-09-01" }, "The range may cover at most 400 days"],
  ])("ignores an invalid range (%o) and says why", async (search, message) => {
    await renderOverview(search);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("The date range was ignored");
    expect(alert).toHaveTextContent(message);
    expect(api.getAnalyticsSummary).toHaveBeenCalledWith({});
    // The rejected text stays in the field so it can be corrected.
    expect(screen.getByLabelText("Start date")).toHaveValue(search.start);
  });

  it("shows the exact make commands when nothing has been imported", async () => {
    api.getAnalyticsSummary.mockResolvedValue(
      ok(
        analyticsSummary({
          sources: [sourceFreshness({ latest_date: null, last_import_id: null, last_imported_at: null, last_via: null })],
          totals_by_source: {},
          daily: [],
          top_links: [],
          live_links: 0,
          links_with_data: 0,
        }),
      ),
    );
    api.listAnalyticsImports.mockResolvedValue(ok(list([])));
    const { container } = await renderOverview();
    const text = container.textContent ?? "";
    expect(text).toContain("make analytics-import SOURCE=first_party DATASET=links");
    expect(text).toContain("make analytics-aggregate");
    expect(text).toContain("make analytics-demo-seed");
    expect(screen.queryByLabelText("Key figures")).toBeNull();
    // Freshness and the (empty) ledger are still shown, so the state is explicit.
    expect(screen.getByRole("table", { name: /Source freshness/ })).toHaveTextContent("Never imported");
    expect(screen.getByRole("table", { name: /Recent imports/ })).toHaveTextContent("No import has succeeded yet.");
  });

  it("shows no CTR rate at all when there were no impressions", async () => {
    api.getAnalyticsSummary.mockResolvedValue(
      ok(
        analyticsSummary({
          totals_by_source: { first_party: metricTotals({ impressions: 0, clicks: 0, ctr: null }) },
          daily: [],
          top_links: [],
        }),
      ),
    );
    await renderOverview();
    const kpis = screen.getByLabelText("Key figures");
    expect(kpis).toHaveTextContent("—");
    expect(kpis).not.toHaveTextContent("0.00%");
    expect(kpis).toHaveTextContent("No impressions in range");
  });

  it("banners synthetic data from the summary, from a top link or from the ledger alone", async () => {
    api.getAnalyticsSummary.mockResolvedValue(ok(analyticsSummary({ synthetic: true })));
    const fromSummary = await renderOverview();
    expect(screen.getByRole("alert")).toHaveTextContent("Synthetic demo data");
    fromSummary.unmount();

    api.getAnalyticsSummary.mockResolvedValue(ok(analyticsSummary({ top_links: [linkPerformanceView({ synthetic: true })] })));
    const fromLink = await renderOverview();
    expect(screen.getByRole("alert")).toHaveTextContent("Synthetic demo data");
    expect(within(screen.getByRole("table", { name: /Most clicked links/ })).getByText("Synthetic")).toBeInTheDocument();
    fromLink.unmount();

    api.getAnalyticsSummary.mockResolvedValue(ok(analyticsSummary()));
    api.listAnalyticsImports.mockResolvedValue(ok(list([analyticsImportView({ synthetic: true, via: "synthetic" })])));
    const fromLedger = await renderOverview();
    expect(screen.getByRole("alert")).toHaveTextContent("Synthetic demo data");
    fromLedger.unmount();

    api.listAnalyticsImports.mockResolvedValue(ok(list([analyticsImportView()])));
    await renderOverview();
    expect(screen.queryByText("Synthetic demo data")).toBeNull();
  });

  it("renders a failed summary, a failed source query and a failed ledger as error states", async () => {
    api.getAnalyticsSummary.mockResolvedValue(fail({ kind: "network" }));
    api.listAnalyticsImports.mockResolvedValue(fail({ kind: "timeout", path: "/analytics/imports" }));
    const both = await renderOverview();
    const alerts = screen.getAllByRole("alert");
    expect(alerts.map((alert) => alert.textContent).join(" ")).toContain("Analytics summary unavailable");
    expect(alerts.map((alert) => alert.textContent).join(" ")).toContain("Imports unavailable");
    both.unmount();

    api.getAnalyticsSummary.mockResolvedValue(ok(analyticsSummary()));
    api.listAnalyticsImports.mockResolvedValue(ok(list([analyticsImportView()])));
    api.listLinkPerformance.mockResolvedValue(fail({ kind: "network", path: "/analytics/links" }));
    await renderOverview({ source: "first_party" });
    expect(screen.getByRole("alert")).toHaveTextContent("Link performance unavailable");
  });

  it("renders model and provider text as text, and never links a javascript: URL", async () => {
    api.getAnalyticsSummary.mockResolvedValue(
      ok(analyticsSummary({ top_links: [linkPerformanceView({ anchor_text: XSS, target_url: "javascript:alert(1)" })] })),
    );
    api.listAnalyticsImports.mockResolvedValue(ok(list([analyticsImportView({ via: "csv", file_name: XSS })])));
    const { container } = await renderOverview();
    expect(container.textContent).toContain(XSS);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
  });
});

async function renderPageImpact(search: Record<string, string> = {}) {
  return render(
    await PageImpactPage({ params: Promise.resolve({ pageId: OTHER_PAGE_ID }), searchParams: Promise.resolve(search) }),
  );
}

describe("/analytics/pages/[pageId]", () => {
  it("shows the fixed non-causal note, the paired bars per change and the daily rows", async () => {
    await renderPageImpact();
    expect(api.getPageImpact).toHaveBeenCalledWith(OTHER_PAGE_ID, { window_days: 28 });
    const note = screen.getByRole("note", { name: "Observed change, not a causal effect" });
    expect(note).toHaveTextContent("not a causal effect");
    expect(note).toHaveTextContent("not a measure of statistical significance");
    const card = screen.getByRole("article", { name: /Search Console: Clicks around 2026-09-01/ });
    expect(within(card).getByRole("img", { name: /before 5.0, after 7.0/ })).toBeInTheDocument();
    expect(within(card).getByRole("list", { name: /Confounders/ })).toHaveTextContent("Content changed");
    expect(screen.getByRole("table", { name: /Search Console daily rows/ })).toHaveTextContent("2026-09-02");
    expect(screen.getByRole("link", { name: "View this page in the inventory" })).toHaveAttribute(
      "href",
      `/inventory/pages/${OTHER_PAGE_ID}`,
    );
  });

  it("accepts a window inside 7-90 and ignores anything else", async () => {
    const seven = await renderPageImpact({ window_days: "7" });
    expect(api.getPageImpact).toHaveBeenLastCalledWith(OTHER_PAGE_ID, { window_days: 7 });
    expect(screen.getByLabelText("Window (days)")).toHaveValue("28");
    seven.unmount();
    await renderPageImpact({ window_days: "400" });
    expect(api.getPageImpact).toHaveBeenLastCalledWith(OTHER_PAGE_ID, { window_days: 28 });
  });

  it("explains an empty page: no change to compare and no stored rows", async () => {
    api.getPageImpact.mockResolvedValue(ok(pageImpactView({ changes: [], series: {} })));
    await renderPageImpact();
    const statuses = screen.getAllByRole("status").map((element) => element.textContent);
    expect(statuses.join(" ")).toContain("No link has been deployed on or to this page");
    expect(statuses.join(" ")).toContain("No page-level data is stored for this page yet.");
    // The note is fixed: it is shown even when there is nothing to compare.
    expect(screen.getByRole("note", { name: "Observed change, not a causal effect" })).toBeInTheDocument();
  });

  it("states the missing coverage instead of a change", async () => {
    api.getPageImpact.mockResolvedValue(
      ok(
        pageImpactView({
          changes: [
            pageWindowComparison({
              pre: windowStats({ covered_days: 9, mean_per_day: null }),
              delta_per_day: null,
              ratio: null,
              flags: ["insufficient_data", "post_window_truncated"],
            }),
          ],
        }),
      ),
    );
    await renderPageImpact();
    const card = screen.getByRole("article");
    expect(within(card).getByRole("status")).toHaveTextContent("Pre-window has 9 of 28 days");
    expect(card).toHaveTextContent("Not shown");
    expect(within(card).getByRole("list", { name: /Confounders/ })).toHaveTextContent("After-window incomplete");
  });

  it("banners and badges synthetic data", async () => {
    api.getPageImpact.mockResolvedValue(
      ok(pageImpactView({ synthetic: true, changes: [pageWindowComparison({ synthetic: true, flags: ["synthetic_data"] })] })),
    );
    await renderPageImpact();
    expect(screen.getByRole("alert")).toHaveTextContent("Synthetic demo data");
    expect(within(screen.getByRole("article")).getByText("Synthetic")).toBeInTheDocument();
  });

  it("is not found for an unknown page, and shows an error state otherwise", async () => {
    api.getPageImpact.mockResolvedValue(fail({ status: 404, code: "page_not_found", path: "/analytics/pages/{page_id}" }));
    await expect(renderPageImpact()).rejects.toThrow("NEXT_NOT_FOUND");
    api.getPageImpact.mockResolvedValue(fail({ kind: "network", path: "/analytics/pages/{page_id}" }));
    await renderPageImpact();
    expect(screen.getByRole("alert")).toHaveTextContent("Page impact unavailable");
    expect(screen.getByRole("link", { name: "Back to analytics" })).toHaveAttribute("href", "/analytics");
  });
});

describe("plan detail: the performance panel", () => {
  async function renderPlan() {
    return render(await RecommendationDetailPage({ params: Promise.resolve({ planId: PLAN_ID }), searchParams: Promise.resolve({}) }));
  }

  it("shows performance for a link that was on a page: window, totals, series and the chain", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "verified" })));
    await renderPlan();
    expect(api.getLinkPerformance).toHaveBeenCalledWith(PLAN_ID);
    const section = screen.getByRole("heading", { level: 2, name: "Performance" }).closest("section")!;
    expect(section).toHaveTextContent("Live");
    expect(section).toHaveTextContent("Range shown: 2026-09-01 to 2026-09-28");
    expect(within(section).getByRole("table", { name: /Totals by source/ })).toHaveTextContent("2.50%");
    expect(within(section).getByRole("img", { name: /Clicks per day: 3 days/ })).toBeInTheDocument();
    expect(within(section).getByRole("list", { name: "Attribution chain" })).toHaveTextContent("accept");
    expect(section).toHaveTextContent("First-party counts are collected from browsers");
  });

  it("asks for no performance at all for a plan that was never applied", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "awaiting_approval" })));
    await renderPlan();
    expect(api.getLinkPerformance).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { level: 2, name: "Performance" })).toBeNull();
  });

  it("recognises a plan that moved on from deployed through its history", async () => {
    api.getLinkPlan.mockResolvedValue(
      ok(linkPlanDetail({ status: "expired", transitions: [transition(), transition({ to_status: "deployed", id: 2 })] })),
    );
    await renderPlan();
    expect(api.getLinkPerformance).toHaveBeenCalledWith(PLAN_ID);
  });

  it("says a rolled-back link's window has closed, and that nothing was recorded", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "rolled_back" })));
    api.getLinkPerformance.mockResolvedValue(
      ok(
        linkPerformanceDetail({
          live_window: { plan_id: PLAN_ID, deployed_at: "2026-09-01T08:00:00Z", rolled_back_at: "2026-09-10T08:00:00Z" },
          series: {},
          totals: { first_party: metricTotals({ impressions: 0, clicks: 0, ctr: null }) },
        }),
      ),
    );
    await renderPlan();
    const section = screen.getByRole("heading", { level: 2, name: "Performance" }).closest("section")!;
    expect(section).toHaveTextContent("Window closed");
    expect(within(section).getByRole("status")).toHaveTextContent(
      "Its live window has closed and no events were recorded while it was open.",
    );
  });

  it("says a live link with no events has none, and shows the measurement record when there is one", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "measured" })));
    api.getLinkPerformance.mockResolvedValue(
      ok(
        linkPerformanceDetail({
          series: {},
          totals: {},
          measurement: {
            plan_id: PLAN_ID,
            measured_at: "2026-09-30T02:00:00Z",
            analytics_policy_version: 1,
            window_start: "2026-09-01",
            window_end: "2026-09-29",
            link_totals: {},
            target_comparison: [pageWindowComparison()],
            source_comparison: [],
            flags: [],
            import_ids: [],
          },
          target_impact: [pageWindowComparison()],
        }),
      ),
    );
    await renderPlan();
    const section = screen.getByRole("heading", { level: 2, name: "Performance" }).closest("section")!;
    expect(within(section).getAllByRole("status")[0]).toHaveTextContent("Live since 2026-09-01 — no events recorded");
    expect(section).toHaveTextContent("No source holds a row for this link in this range.");
    expect(section).toHaveTextContent("analytics policy version 1");
    expect(within(section).getByRole("note", { name: "Observed change, not a causal effect" })).toBeInTheDocument();
    expect(within(section).getByRole("article", { name: /Search Console: Clicks/ })).toBeInTheDocument();
  });

  it("explains that a never-live plan can hold no data, and banners synthetic data", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "rolled_back" })));
    api.getLinkPerformance.mockResolvedValue(
      ok(
        linkPerformanceDetail({
          live_window: { plan_id: PLAN_ID, deployed_at: null, rolled_back_at: null },
          series: {},
          totals: {},
          synthetic: true,
        }),
      ),
    );
    await renderPlan();
    const section = screen.getByRole("heading", { level: 2, name: "Performance" }).closest("section")!;
    expect(section).toHaveTextContent("Never on a page");
    expect(within(section).getByRole("alert")).toHaveTextContent("Synthetic demo data");
    expect(within(section).getAllByText("Synthetic").length).toBeGreaterThan(0);
  });

  it("shows a failed performance call without hiding the recommendation", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "verified" })));
    api.getLinkPerformance.mockResolvedValue(fail({ kind: "network", path: "/analytics/links/{plan_id}" }));
    await renderPlan();
    const section = screen.getByRole("heading", { level: 2, name: "Performance" }).closest("section")!;
    expect(within(section).getByRole("alert")).toHaveTextContent("Performance unavailable");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Recommendation");
  });
});

describe("deployment detail: the per-item performance table", () => {
  async function renderDeployment() {
    return render(
      await DeploymentDetailPage({
        params: Promise.resolve({ deploymentId: DEPLOYMENT_ID }),
        searchParams: Promise.resolve({}),
      }),
    );
  }

  it("asks for the links of this deployment and tabulates them", async () => {
    api.getDeployment.mockResolvedValue(ok(deploymentDetail()));
    await renderDeployment();
    expect(api.listLinkPerformance).toHaveBeenCalledWith({ deployment_id: DEPLOYMENT_ID });
    const table = screen.getByRole("table", { name: /Performance of the links in this deployment/ });
    expect(table).toHaveTextContent("1,000");
    expect(table).toHaveTextContent("2.50%");
    expect(screen.getByRole("link", { name: "analytics overview" })).toHaveAttribute("href", "/analytics");
  });

  it("has an explicit empty state and banners synthetic rows", async () => {
    api.getDeployment.mockResolvedValue(ok(deploymentDetail()));
    api.listLinkPerformance.mockResolvedValue(ok(list([])));
    const empty = await renderDeployment();
    expect(screen.getByRole("table", { name: /Performance of the links/ })).toHaveTextContent(
      "No analytics row is stored for these links yet.",
    );
    empty.unmount();
    api.listLinkPerformance.mockResolvedValue(ok(list([linkPerformanceView({ synthetic: true })])));
    await renderDeployment();
    expect(screen.getByRole("alert")).toHaveTextContent("Synthetic demo data");
  });

  it("shows a failed performance call without hiding the deployment", async () => {
    api.getDeployment.mockResolvedValue(ok(deploymentDetail()));
    api.listLinkPerformance.mockResolvedValue(fail({ kind: "network", path: "/analytics/links" }));
    await renderDeployment();
    const section = screen.getByRole("heading", { level: 2, name: "Performance" }).closest("section")!;
    expect(within(section).getByRole("alert")).toHaveTextContent("Performance unavailable");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Deployment to");
  });
});

describe("rendering safety", () => {
  it("charts carry a <title> and a table of the same numbers", async () => {
    api.getPageImpact.mockResolvedValue(ok(pageImpactView({ series: { ga4: pageDailyPoints(2) } })));
    const { container } = await renderPageImpact();
    const titles = [...container.querySelectorAll("svg")].map((svg) => svg.querySelector("title")?.textContent ?? "");
    expect(titles.length).toBeGreaterThan(0);
    for (const title of titles) expect(title.length).toBeGreaterThan(10);
    expect(container.querySelectorAll("svg").length).toBeLessThanOrEqual(container.querySelectorAll("table").length);
  });

  it("the link series chart and its table agree", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "verified" })));
    api.getLinkPerformance.mockResolvedValue(ok(linkPerformanceDetail({ series: { first_party: linkDailyPoints(2) } })));
    render(await RecommendationDetailPage({ params: Promise.resolve({ planId: PLAN_ID }), searchParams: Promise.resolve({}) }));
    const table = screen.getByRole("table", { name: /First-party daily totals for this link \(daily values\)/ });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(screen.getByRole("img", { name: /Clicks per day: 2 days/ })).toBeInTheDocument();
  });
});
