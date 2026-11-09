import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PageDetailPage from "@/app/inventory/pages/[pageId]/page";
import RecommendationDetailPage from "@/app/recommendations/[planId]/page";
import CandidatesPage from "@/app/recommendations/candidates/page";
import ModulesPage from "@/app/recommendations/modules/page";
import OpportunitiesPage from "@/app/recommendations/opportunities/page";
import RecommendationsPage from "@/app/recommendations/page";
import type { ApiError, ApiResult } from "@/lib/api-client";

import { linkPerformanceDetail } from "./fixtures/analytics";
import { pageIntelligence, taxonomy } from "./fixtures/intelligence";
import { OTHER_PAGE_ID, PAGE_ID, list, pageDetail, versionSummary } from "./fixtures/inventory";
import {
  ANCHOR,
  OLD_PLAN_ID,
  PLAN_ID,
  REC_RUN_ID,
  candidate,
  linkPlan,
  linkPlanDetail,
  opportunity,
  pageModule,
  recommendationSummary,
  transition,
} from "./fixtures/recommendations";
import { principal } from "./fixtures/review";
import { autonomyHealth } from "./fixtures/autonomy";

const api = vi.hoisted(() => ({
  // Phase 7: the plan detail screen's performance panel.
  getLinkPerformance: vi.fn(),
  // Phase 6: the plan detail screen's deployment panel.
  listPlanDeployments: vi.fn(),
  listLinkPlans: vi.fn(),
  getLinkPlan: vi.fn(),
  listLinkCandidates: vi.fn(),
  listOpportunities: vi.fn(),
  listPageModules: vi.fn(),
  getRecommendationSummary: vi.fn(),
  // ADR-0018: the recommendations list carries the autonomy-mode banner.
  getAutonomyMode: vi.fn(),
  // The plan detail screen's agent review panel (Phase 4).
  listAgentPlanReviews: vi.fn(),
  // Phase 5: the session user, review history and replacement targets.
  requireSessionUser: vi.fn(),
  listPlanReviewActions: vi.fn(),
  listPlanTargetOptions: vi.fn(),
  // The page detail screen's other calls.
  getInventoryPage: vi.fn(),
  getInventoryPageVersion: vi.fn(),
  listInventoryPageVersions: vi.fn(),
  listInventoryPageLinks: vi.fn(),
  listInventoryLinks: vi.fn(),
  getPageIntelligence: vi.fn(),
  listPageRelationships: vi.fn(),
  listSimilarPages: vi.fn(),
  getTaxonomy: vi.fn(),
}));

const nav = vi.hoisted(() => ({
  notFound: vi.fn(() => {
    throw new Error("NEXT_NOT_FOUND");
  }),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

vi.mock("next/navigation", async (importOriginal) => ({
  ...(await importOriginal<typeof import("next/navigation")>()),
  notFound: nav.notFound,
}));

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, status: 200 };
}

function fail(error: Partial<ApiError> & Pick<ApiError, "kind">): ApiResult<never> {
  return { ok: false, error: { path: "/recommendations", message: "It failed.", ...error } };
}

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  api.getLinkPerformance.mockResolvedValue({ ok: true, data: linkPerformanceDetail(), status: 200 });
  api.listPlanDeployments.mockResolvedValue({ ok: true, data: [], status: 200 });
  nav.notFound.mockClear();
  api.getRecommendationSummary.mockResolvedValue(ok(recommendationSummary()));
  api.listAgentPlanReviews.mockResolvedValue(ok([]));
  // These screens are tested as a read-only viewer; reviewer flows live in review-screens.test.tsx.
  api.requireSessionUser.mockResolvedValue(ok(principal("viewer")));
  api.listPlanReviewActions.mockResolvedValue(ok([]));
  api.listPlanTargetOptions.mockResolvedValue(ok([]));
  api.getAutonomyMode.mockResolvedValue(ok(autonomyHealth()));
});

async function renderPage(element: Promise<React.ReactElement>) {
  return render(await element);
}

const XSS = '<script>window.__pwned = true</script><img src=x onerror="alert(1)">';

function expectNoInjectedMarkup(container: HTMLElement) {
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
}

describe("/recommendations", () => {
  // ADR-0018 §5: a monitoring list, so the default is EVERY status and the
  // request must not narrow to the approval queue.
  it("defaults to every status and renders the table with a raw-confidence label", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan()], { total: 120, has_more: true })));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    expect(api.listLinkPlans).toHaveBeenCalledWith({
      status: null,
      min_score: null,
      source_page_id: null,
      target_page_id: null,
      limit: 50,
      offset: 0,
    });
    const table = screen.getByRole("table", { name: /recommendations \(every status\)/i });
    const row = within(table).getAllByRole("row")[1]!;
    expect(within(row).getByRole("link", { name: `${ANCHOR} — view recommendation` })).toHaveAttribute(
      "href",
      `/recommendations/${PLAN_ID}`,
    );
    expect(row).toHaveTextContent("0.630");
    expect(within(row).getByText("raw")).toBeInTheDocument();
    expect(row).not.toHaveTextContent(/\bcalibrated\b(?! confidence)/);
    expect(within(row).getByRole("link", { name: "Lithium-ion battery market report" })).toHaveAttribute(
      "href",
      `/inventory/pages/${OTHER_PAGE_ID}`,
    );
    expect(within(row).getByRole("link", { name: "https://www.example.com/reports/widgets" })).toHaveAttribute(
      "href",
      `/inventory/pages/${PAGE_ID}`,
    );
    expect(row).toHaveTextContent("Related market");
    expect(row).toHaveTextContent("Semantically similar content");
    expect(row).toHaveTextContent("+1 more");
    expect(within(row).getByText("Proposed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /next/i })).toHaveAttribute("href", "/recommendations?offset=50");
  });

  it("labels calibrated confidence only when a calibration version exists", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan({ calibration_version: "isotonic-1" })])));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    const row = within(screen.getByRole("table", { name: /recommendations/i })).getAllByRole("row")[1]!;
    expect(within(row).getByText("calibrated")).toBeInTheDocument();
    expect(within(row).queryByText("raw")).toBeNull();
  });

  it("passes filters from the query string and keeps them across pages", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan()], { total: 80, offset: 50, has_more: true })));
    await renderPage(
      RecommendationsPage({
        searchParams: Promise.resolve({
          status: "all",
          min_score: "0.4",
          source_page_id: PAGE_ID,
          target_page_id: OTHER_PAGE_ID,
          offset: "50",
        }),
      }),
    );
    expect(api.listLinkPlans).toHaveBeenCalledWith({
      status: null,
      min_score: 0.4,
      source_page_id: PAGE_ID,
      target_page_id: OTHER_PAGE_ID,
      limit: 50,
      offset: 50,
    });
    expect(screen.getByRole("table", { name: /every status/i })).toBeInTheDocument();
    expect(screen.getByLabelText("Status")).toHaveValue("all");
    expect(screen.getByLabelText("Minimum score")).toHaveValue(0.4);
    const prev = new URL(screen.getByRole("link", { name: /previous/i }).getAttribute("href")!, "http://x");
    // `all` is now the DEFAULT, so it is omitted rather than repeated; the
    // other filters must still survive the page change.
    expect(prev.searchParams.get("status")).toBeNull();
    expect(prev.searchParams.get("min_score")).toBe("0.4");
    expect(prev.searchParams.get("source_page_id")).toBe(PAGE_ID);
    expect(screen.getByRole("link", { name: "Clear filters" })).toHaveAttribute("href", "/recommendations");
  });

  it("carries a non-default status across pages", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan()], { total: 80, offset: 50, has_more: true })));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({ status: "blocked", offset: "50" }) }));
    expect(api.listLinkPlans).toHaveBeenCalledWith(expect.objectContaining({ status: "blocked" }));
    const prev = new URL(screen.getByRole("link", { name: /previous/i }).getAttribute("href")!, "http://x");
    expect(prev.searchParams.get("status")).toBe("blocked");
  });

  it("ignores invalid filters, says so, and does not send them", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([])));
    await renderPage(
      RecommendationsPage({ searchParams: Promise.resolve({ min_score: "2", source_page_id: "../x", status: "bogus" }) }),
    );
    expect(api.listLinkPlans).toHaveBeenCalledWith(
      expect.objectContaining({ status: null, min_score: null, source_page_id: null }),
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Minimum score must be a number from 0 to 1.");
    expect(alert).toHaveTextContent("Source page id must be a page UUID.");
  });

  it("shows the 'nothing yet' empty state when no plan exists at all", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([])));
    api.getRecommendationSummary.mockResolvedValue(ok(recommendationSummary({ plans_by_status: {}, last_run_id: null })));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "No recommendations yet — run make recommend after make analyze.",
    );
  });

  it("says the filters excluded everything when plans exist elsewhere", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([])));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({ status: "blocked" }) }));
    expect(screen.getByRole("status")).toHaveTextContent("No recommendations match these filters.");
    // The old wording named the approval queue as the default; it no longer is.
    expect(screen.getByRole("status")).not.toHaveTextContent("awaiting approval");
  });

  it("renders the dashboard summary with counts, links and policy versions", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan()])));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    const overview = screen.getByRole("heading", { level: 2, name: "Overview" }).closest("section")!;
    expect(within(overview).getByText("Plans (all statuses)").nextElementSibling).toHaveTextContent("15");
    expect(within(overview).getByText("Source pages with plans").nextElementSibling).toHaveTextContent("6");
    expect(within(overview).getByText("Ranking policy").nextElementSibling).toHaveTextContent("version 1");
    const byStatus = within(overview).getByRole("table", { name: /plans by status/i });
    expect(within(byStatus).getAllByRole("rowheader").map((h) => h.firstChild?.textContent)).toEqual([
      "Proposed",
      "Approved (exception)",
      "Rejected",
    ]);
    expect(within(byStatus).getByRole("link", { name: "Approved (exception)" })).toHaveAttribute(
      "href",
      "/recommendations?status=approved",
    );
    const byKind = within(overview).getByRole("table", { name: /opportunities by kind/i });
    expect(within(byKind).getByRole("link", { name: "Link capacity" })).toHaveAttribute(
      "href",
      "/recommendations/opportunities#link_capacity",
    );
    expect(within(overview).getByRole("link", { name: REC_RUN_ID })).toHaveAttribute("href", `/jobs/${REC_RUN_ID}`);
  });

  it("renders API errors without crashing", async () => {
    api.listLinkPlans.mockResolvedValue(fail({ kind: "network" }));
    api.getRecommendationSummary.mockResolvedValue(fail({ kind: "timeout", path: "/recommendations/summary" }));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    const alerts = screen.getAllByRole("alert");
    expect(alerts.map((a) => a.querySelector("h3")?.textContent)).toEqual([
      "✕ API timed out: Recommendation summary unavailable",
      "✕ API unreachable: Recommendations unavailable",
    ]);
  });

  it("renders anchor and title text from the API as text, never HTML", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan({ anchor_text: XSS, target_title: XSS })])));
    const { container } = await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    expectNoInjectedMarkup(container);
    expect(screen.getAllByText(XSS, { exact: false }).length).toBeGreaterThan(0);
  });

  it("offers no mutating controls to a viewer", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan()])));
    const { container } = await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.queryByRole("button", { name: /approve|reject|edit|deploy/i })).toBeNull();
    for (const form of container.querySelectorAll("form")) expect(form.getAttribute("method")).toBe("get");
  });
});

describe("/recommendations/[planId]", () => {
  async function renderDetail() {
    return renderPage(RecommendationDetailPage({ params: Promise.resolve({ planId: PLAN_ID }) }));
  }

  it("highlights the anchor in the before/after preview and the source block", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail()));
    const { container } = await renderDetail();
    expect(api.getLinkPlan).toHaveBeenCalledWith(PLAN_ID);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(`Recommendation: link “${ANCHOR}”`);
    const preview = screen.getByRole("heading", { level: 2, name: "Placement preview" }).closest("section")!;
    const before = within(preview).getByText("Before (current text)").closest("figure")!;
    const after = within(preview).getByText("After (with the proposed link)").closest("figure")!;
    expect(before.querySelector("mark")).toBeNull();
    expect(before).toHaveTextContent(`The global ${ANCHOR} is growing as electric vehicles scale.`);
    expect(after.querySelector("mark")).toHaveTextContent(ANCHOR);
    const block = screen.getByRole("region", { name: "Source block text" });
    expect(block.querySelector("mark")).toHaveTextContent(ANCHOR);
    expect(container.querySelectorAll("mark")).toHaveLength(2);
  });

  it("marks omitted context with an ellipsis and warns when offsets do not match", async () => {
    const long = `${"x".repeat(300)} ${ANCHOR} tail text beyond the preview`;
    api.getLinkPlan.mockResolvedValue(
      ok(
        linkPlanDetail({
          source_block_text: long,
          locator: { ...linkPlan().locator, start: 5, end: 31 },
          preview: { before: "x ", anchor: ANCHOR, after: " tail" },
        }),
      ),
    );
    await renderDetail();
    const after = screen.getByText("After (with the proposed link)").closest("figure")!;
    expect(after.textContent?.startsWith("After (with the proposed link)…")).toBe(true);
    expect(after.textContent?.endsWith("tail…")).toBe(true);
    const block = screen.getByRole("region", { name: "Source block text" });
    expect(block.querySelector("mark")).toBeNull();
    expect(
      screen.getAllByRole("note").some((note) => note.textContent?.includes("do not select the anchor text") === true),
    ).toBe(true);
  });

  it("never injects HTML from anchor, preview, block or note text", async () => {
    const block = `Intro ${XSS} outro`;
    api.getLinkPlan.mockResolvedValue(
      ok(
        linkPlanDetail({
          anchor_text: XSS,
          source_block_text: block,
          locator: { ...linkPlan().locator, start: 6, end: 6 + Array.from(XSS).length },
          preview: { before: "Intro ", anchor: XSS, after: " outro" },
          rationale: XSS,
          source_title: XSS,
          transitions: [transition({ note: XSS, actor: `user:${XSS}` })],
        }),
      ),
    );
    const { container } = await renderDetail();
    expectNoInjectedMarkup(container);
    expect(screen.getByRole("region", { name: "Source block text" }).querySelector("mark")?.textContent).toBe(XSS);
    expect(screen.getAllByText(XSS, { exact: false }).length).toBeGreaterThanOrEqual(4);
  });

  it("shows the locator, score breakdown, reasons and provenance", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail()));
    await renderDetail();
    const block = screen.getByRole("heading", { level: 2, name: "Source block" }).closest("section")!;
    expect(within(block).getByText("Block id (hint)").nextElementSibling).toHaveTextContent("b4");
    expect(within(block).getByText("Occurrence").nextElementSibling).toHaveTextContent("1 (index 0)");
    expect(within(block).getByText("Offsets").nextElementSibling).toHaveTextContent("11–37");
    expect(within(block).getByText("Context hash").nextElementSibling).toHaveTextContent("a".repeat(12));

    const score = screen.getByRole("table", { name: "Score breakdown" });
    const semantic = within(score).getByRole("rowheader", { name: /^Semantic semantic/ }).closest("tr")!;
    expect(within(semantic).getAllByRole("cell").map((c) => c.textContent)).toEqual(["0.800", "0.500", "0.400"]);
    expect(within(score).getByRole("rowheader", { name: /Relevance/ }).closest("tr")).toHaveTextContent("0.700");
    expect(within(score).getByRole("rowheader", { name: /Placement quality/ }).closest("tr")).toHaveTextContent("0.900");
    expect(within(score).getByRole("rowheader", { name: /^Total/ }).closest("tr")).toHaveTextContent("0.630");
    const scoreSection = score.closest("section")!;
    expect(within(scoreSection).getByText("Score adds up")).toBeInTheDocument();
    expect(within(scoreSection).getByText("Semantic floor").nextElementSibling).toHaveTextContent("0.350");
    expect(within(scoreSection).getByText("Ranking policy").nextElementSibling).toHaveTextContent(
      "ranking_weights version 1",
    );

    const reasons = screen.getByRole("list", { name: "Reason codes" });
    expect(within(reasons).getAllByRole("listitem")).toHaveLength(4);
    expect(reasons).toHaveTextContent("Anchor matches the target's topic");
    expect(screen.getByText(/The target covers a related market/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /why these targets/i })).toHaveAttribute(
      "href",
      `/recommendations/candidates?source_page_id=${PAGE_ID}&run_id=${REC_RUN_ID}`,
    );

    const provenance = screen.getByRole("heading", { level: 2, name: "Provenance" }).closest("section")!;
    expect(within(provenance).getByText("Engine version").nextElementSibling).toHaveTextContent("recommend-1.0.0");
    expect(within(provenance).getByText("Embedding model").nextElementSibling).toHaveTextContent("nomic-embed-text");
    expect(within(provenance).getByText("Reasoning model").nextElementSibling).toHaveTextContent(
      "None (deterministic; no model reasoning)",
    );
    expect(within(provenance).getByText("Calibration").nextElementSibling).toHaveTextContent("None (confidence is raw)");
    expect(within(provenance).getByText("Source version").nextElementSibling).toHaveTextContent("v2");
    expect(within(provenance).getByText("Target version").nextElementSibling).toHaveTextContent("v5");
    expect(within(provenance).getByRole("link", { name: REC_RUN_ID })).toHaveAttribute("href", `/jobs/${REC_RUN_ID}`);

    const pages = screen.getByRole("heading", { level: 2, name: "Pages" }).closest("section")!;
    expect(within(pages).getByRole("link", { name: "Widgets market report" })).toHaveAttribute(
      "href",
      `/inventory/pages/${PAGE_ID}`,
    );
    expect(within(pages).getByRole("link", { name: "Lithium-ion battery market report" })).toHaveAttribute(
      "href",
      `/inventory/pages/${OTHER_PAGE_ID}`,
    );
    expect(screen.getByText("raw")).toBeInTheDocument();
  });

  it("flags a score breakdown that does not add up", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ score: { ...linkPlan().score, total: 0.9 } })));
    await renderDetail();
    expect(screen.getByText("Score does not add up")).toBeInTheDocument();
  });

  it("shows the status history and the superseded plan", async () => {
    api.getLinkPlan.mockResolvedValue(
      ok(
        linkPlanDetail({
          status: "awaiting_approval",
          supersedes: OLD_PLAN_ID,
          anchor_source: "edited",
          transitions: [
            transition(),
            transition({
              id: 2,
              from_status: "proposed",
              to_status: "awaiting_approval",
              actor: "agent:critic@1.0.0",
              note: "Passed critique.",
              occurred_at: "2026-10-14T09:00:00Z",
            }),
          ],
        }),
      ),
    );
    await renderDetail();
    const header = screen.getByRole("heading", { level: 1 }).parentElement!;
    expect(within(header).getByText("Awaiting approval (exception)")).toBeInTheDocument();
    expect(within(header).getByRole("link", { name: OLD_PLAN_ID })).toHaveAttribute(
      "href",
      `/recommendations/${OLD_PLAN_ID}`,
    );
    const history = screen.getByRole("table", { name: /status history/i });
    const rows = within(history).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("(created)");
    expect(rows[0]).toHaveTextContent("System recommendation");
    expect(rows[1]).toHaveTextContent("2026-10-14 09:00:00 UTC");
    const [, from, to] = within(rows[1]!).getAllByRole("cell");
    expect(from).toHaveTextContent("Proposed");
    expect(to).toHaveTextContent("Awaiting approval");
    expect(rows[1]).toHaveTextContent("Agent critic@1.0.0");
    expect(rows[1]).toHaveTextContent("Passed critique.");
    expect(screen.getByText("Anchor source").nextElementSibling).toHaveTextContent("Edited by a reviewer");
  });

  it("calls notFound for a 404, a 422 and a malformed id", async () => {
    for (const result of [
      fail({ kind: "api_error", status: 404, code: "not_found", path: "/recommendations/{plan_id}" }),
      fail({ kind: "api_error", status: 422, code: "validation_failed", path: "/recommendations/{plan_id}" }),
      fail({ kind: "invalid_argument", path: "/recommendations/{plan_id}" }),
    ]) {
      api.getLinkPlan.mockResolvedValueOnce(result);
      await expect(renderDetail()).rejects.toThrow("NEXT_NOT_FOUND");
    }
    expect(nav.notFound).toHaveBeenCalledTimes(3);
  });

  it("renders other failures as an error notice", async () => {
    api.getLinkPlan.mockResolvedValue(fail({ kind: "invalid_body", status: 200, path: "/recommendations/{plan_id}" }));
    await renderDetail();
    expect(screen.getByRole("alert")).toHaveTextContent("API response did not match the contract: Recommendation unavailable");
    expect(nav.notFound).not.toHaveBeenCalled();
  });
});

describe("/recommendations/opportunities", () => {
  it("lists each kind with score and evidence, linking to page detail", async () => {
    api.listOpportunities.mockImplementation(async ({ kind }: { kind: string }) =>
      kind === "orphan"
        ? ok(list([opportunity({ evidence: { inbound_links: 0, api_token: "secret", markets: ["ev", "grid"] } })]))
        : kind === "underlinked"
          ? ok(list([opportunity({ kind: "underlinked", score: 0.5, title: null })], { total: 70, has_more: true }))
          : ok(list([])),
    );
    await renderPage(OpportunitiesPage({ searchParams: Promise.resolve({ link_capacity_offset: "50" }) }));
    expect(api.listOpportunities.mock.calls.map(([query]) => query)).toEqual([
      { kind: "orphan", limit: 50, offset: 0 },
      { kind: "underlinked", limit: 50, offset: 0 },
      { kind: "link_capacity", limit: 50, offset: 50 },
    ]);
    const orphan = screen.getByRole("table", { name: /orphan pages/i });
    expect(within(orphan).getByRole("link", { name: "Lithium-ion battery market report" })).toHaveAttribute(
      "href",
      `/inventory/pages/${OTHER_PAGE_ID}`,
    );
    expect(orphan).toHaveTextContent("0.900");
    expect(orphan).toHaveTextContent("inbound_links0");
    expect(orphan).toHaveTextContent('markets["ev","grid"]');
    expect(orphan).toHaveTextContent("api_token[withheld]");
    expect(orphan).not.toHaveTextContent("secret");
    const under = screen.getByRole("table", { name: /under-linked pages/i });
    expect(within(under).getByRole("link", { name: "https://www.example.com/reports/lithium-ion-battery-market" })).toBeInTheDocument();
    const underNext = screen.getByRole("navigation", { name: "Under-linked pages pagination" });
    expect(within(underNext).getByRole("link", { name: /next/i })).toHaveAttribute(
      "href",
      "/recommendations/opportunities?underlinked_offset=50&link_capacity_offset=50",
    );
    expect(screen.getByRole("table", { name: /link capacity/i })).toHaveTextContent(
      "No eligible source pages with spare link capacity.",
    );
  });

  it("keeps the other sections when one call fails", async () => {
    api.listOpportunities.mockImplementation(async ({ kind }: { kind: string }) =>
      kind === "underlinked" ? fail({ kind: "network", path: "/recommendations/opportunities" }) : ok(list([])),
    );
    await renderPage(OpportunitiesPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("API unreachable: Under-linked pages unavailable");
    expect(screen.getByRole("table", { name: /orphan pages/i })).toBeInTheDocument();
  });
});

describe("/recommendations/modules", () => {
  it("lists each module's items in rank order, with geography and reasons", async () => {
    api.listPageModules.mockResolvedValue(ok(list([pageModule()])));
    await renderPage(ModulesPage({ searchParams: Promise.resolve({}) }));
    expect(api.listPageModules.mock.calls.map(([query]) => query)).toEqual([{ limit: 50, offset: 0 }]);
    const table = screen.getByRole("table", { name: /related-report modules/i });
    const items = within(table).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(within(items[0]!).getByRole("link", { name: "Asia Pacific electric vehicle market" })).toHaveAttribute(
      "href",
      `/inventory/pages/${OTHER_PAGE_ID}`,
    );
    expect(items[0]).toHaveTextContent("Asia Pacific");
    expect(items[0]).toHaveTextContent("Same market, another geography");
    // A global target has no geography label and falls back to its URL for link text.
    expect(within(items[1]!).getByRole("link", { name: /global-electric-vehicle-market/ })).toBeInTheDocument();
    expect(items[1]).toHaveTextContent("Target covers the market globally");
    expect(table).toHaveTextContent("tax3-alg2");
  });

  it("says what to run when nothing is proposed, and reports an API failure", async () => {
    api.listPageModules.mockResolvedValue(ok(list([])));
    await renderPage(ModulesPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("table", { name: /related-report modules/i })).toHaveTextContent("make modules");

    api.listPageModules.mockResolvedValue(fail({ kind: "network", path: "/recommendations/modules" }));
    await renderPage(ModulesPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("Related-report modules unavailable");
  });
});

describe("/recommendations/candidates", () => {
  it("lists kept and rejected candidates with labelled rejection codes", async () => {
    api.listLinkCandidates.mockResolvedValue(
      ok(list([candidate(), candidate({ target_page_id: PAGE_ID, rejection: "already_linked", plan_id: null, total_score: null })])),
    );
    await renderPage(
      CandidatesPage({ searchParams: Promise.resolve({ source_page_id: PAGE_ID, run_id: REC_RUN_ID, outcome: "rejected" }) }),
    );
    expect(api.listLinkCandidates).toHaveBeenCalledWith({
      source_page_id: PAGE_ID,
      run_id: REC_RUN_ID,
      rejected: true,
      limit: 50,
      offset: 0,
    });
    const rows = within(screen.getByRole("table", { name: /candidates/i })).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Kept");
    expect(rows[0]).toHaveTextContent("Semantic (vector index)0.810");
    // ADR-0018 per-channel attribution: the per-signal numbers stay visible
    // rather than collapsing into the total alone.
    expect(rows[0]).toHaveTextContent("raw 0.810");
    expect(rows[0]).toHaveTextContent("normalised 0.810");
    expect(rows[0]).toHaveTextContent("rank 1");
    expect(rows[0]).toHaveTextContent("Lexical (full text)0.420");
    expect(rows[0]).toHaveTextContent("rank 4");
    // And the destination's graph position, which is why an orphan is worth linking.
    expect(rows[0]).toHaveTextContent("Nearly orphan");
    expect(rows[0]).toHaveTextContent("in 1 / out 7");
    expect(within(rows[0]!).getByRole("link", { name: "View plan" })).toHaveAttribute("href", `/recommendations/${PLAN_ID}`);
    expect(rows[1]).toHaveTextContent("Rejected");
    expect(rows[1]).toHaveTextContent("Source already links to the target");
    expect(rows[1]).toHaveTextContent("already_linked");
  });

  it("ignores an invalid id filter and shows the empty state", async () => {
    api.listLinkCandidates.mockResolvedValue(ok(list([])));
    await renderPage(CandidatesPage({ searchParams: Promise.resolve({ run_id: "nope" }) }));
    expect(api.listLinkCandidates).toHaveBeenCalledWith(expect.objectContaining({ run_id: null, rejected: null }));
    expect(screen.getByRole("alert")).toHaveTextContent("Run id must be a run UUID.");
    expect(screen.getByRole("table", { name: /candidates/i })).toHaveTextContent("No candidates match these filters.");
  });
});

describe("/inventory/pages/[pageId] recommendation sections", () => {
  beforeEach(() => {
    api.getInventoryPage.mockResolvedValue(ok(pageDetail()));
    api.listInventoryPageVersions.mockResolvedValue(ok(list([versionSummary()])));
    api.listInventoryPageLinks.mockResolvedValue(ok(list([])));
    api.listInventoryLinks.mockResolvedValue(ok(list([])));
    api.getPageIntelligence.mockResolvedValue(ok(pageIntelligence()));
    api.listPageRelationships.mockResolvedValue(ok(list([])));
    api.listSimilarPages.mockResolvedValue(ok(list([])));
    api.getTaxonomy.mockResolvedValue(ok(taxonomy()));
  });

  it("lists plans from and to the page in compact tables", async () => {
    api.listLinkPlans.mockImplementation(async (query: { source_page_id?: string }) =>
      query.source_page_id === undefined
        ? ok(list([]))
        : ok(list([linkPlan({ status: "approved" })], { total: 14, has_more: true })),
    );
    await renderPage(PageDetailPage({ params: Promise.resolve({ pageId: PAGE_ID }), searchParams: Promise.resolve({}) }));
    expect(api.listLinkPlans).toHaveBeenCalledWith({ source_page_id: PAGE_ID, limit: 10 });
    expect(api.listLinkPlans).toHaveBeenCalledWith({ target_page_id: PAGE_ID, limit: 10 });
    const from = screen.getByRole("table", { name: /recommendations from this page/i });
    expect(within(from).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Score",
      "Anchor text",
      "Target page",
      "Status",
      "Created",
    ]);
    expect(within(from).getByText("Approved (exception)")).toBeInTheDocument();
    expect(within(from).getByText("raw")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View all 14 recommendations from this page" })).toHaveAttribute(
      "href",
      `/recommendations?status=all&source_page_id=${PAGE_ID}`,
    );
    const to = screen.getByRole("table", { name: /recommendations to this page/i });
    expect(within(to).getAllByRole("columnheader").map((h) => h.textContent)).toContain("Source page");
    expect(to).toHaveTextContent("No recommendations link to this page.");
  });

  it("keeps the page when the recommendation calls fail", async () => {
    api.listLinkPlans.mockResolvedValue(fail({ kind: "timeout" }));
    await renderPage(PageDetailPage({ params: Promise.resolve({ pageId: PAGE_ID }), searchParams: Promise.resolve({}) }));
    expect(screen.getAllByRole("alert").map((a) => a.querySelector("h3")?.textContent)).toEqual([
      "✕ API timed out: Recommendations from this page unavailable",
      "✕ API timed out: Recommendations to this page unavailable",
    ]);
    expect(screen.getByRole("heading", { level: 1, name: "Widgets market report" })).toBeInTheDocument();
  });
});
