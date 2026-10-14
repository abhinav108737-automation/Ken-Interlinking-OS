import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import IntelligencePage from "@/app/intelligence/page";
import SemanticSearchPage from "@/app/intelligence/search/page";
import TaxonomyNodePage from "@/app/intelligence/topics/[nodeId]/page";
import PageDetailPage from "@/app/inventory/pages/[pageId]/page";
import JobRunDetailPage from "@/app/jobs/[runId]/page";
import JobRunsPage from "@/app/jobs/page";
import type { ApiError, ApiResult } from "@/lib/api-client";

import {
  JOB_RUN_ID,
  coverage,
  jobRunDetail,
  jobRunSummary,
  pageIntelligence,
  protection,
  relationship,
  similarPage,
  taxonomy,
} from "./fixtures/intelligence";
import { PAGE_ID, list, pageDetail, versionSummary } from "./fixtures/inventory";

const api = vi.hoisted(() => ({
  // Inventory calls made by the page detail screen.
  getInventoryPage: vi.fn(),
  getInventoryPageVersion: vi.fn(),
  listInventoryPageVersions: vi.fn(),
  listInventoryPageLinks: vi.fn(),
  listInventoryLinks: vi.fn(),
  // Phase 2.
  getIntelligenceCoverage: vi.fn(),
  getTaxonomy: vi.fn(),
  // Phase 3: the page detail screen also lists recommendations from/to the page.
  listLinkPlans: vi.fn(),
  listTaxonomyNodePages: vi.fn(),
  getPageIntelligence: vi.fn(),
  listSimilarPages: vi.fn(),
  listPageRelationships: vi.fn(),
  semanticSearch: vi.fn(),
  listJobRuns: vi.fn(),
  getJobRun: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, status: 200 };
}

function fail(error: Partial<ApiError> & Pick<ApiError, "kind">): ApiResult<never> {
  return { ok: false, error: { path: "/intelligence/coverage", message: "It failed.", ...error } };
}

const NOT_FOUND = fail({ kind: "api_error", status: 404, code: "not_found" });
const MODEL_DOWN = fail({ kind: "api_error", status: 503, code: "model_unavailable", path: "/intelligence/search" });

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  api.getTaxonomy.mockResolvedValue(ok(taxonomy()));
  api.listLinkPlans.mockResolvedValue(ok(list([])));
});

async function renderPage(element: Promise<React.ReactElement>) {
  return render(await element);
}

describe("/inventory/pages/[pageId] intelligence section", () => {
  function mockDetail() {
    api.getInventoryPage.mockResolvedValue(ok(pageDetail()));
    api.listInventoryPageVersions.mockResolvedValue(ok(list([versionSummary()])));
    api.listInventoryPageLinks.mockResolvedValue(ok(list([])));
    api.listInventoryLinks.mockResolvedValue(ok(list([])));
    api.getPageIntelligence.mockResolvedValue(
      ok(
        pageIntelligence({
          protections: [protection()],
          eligibility: { as_source: false, as_target: false, source_reasons: ["protected"], target_reasons: ["protected", "thin_content"] },
        }),
      ),
    );
    api.listPageRelationships.mockResolvedValue(ok(list([relationship()])));
    api.listSimilarPages.mockResolvedValue(ok(list([similarPage()])));
  }

  it("loads intelligence, relationships, similar pages and taxonomy labels for the page", async () => {
    mockDetail();
    await renderPage(
      PageDetailPage({ params: Promise.resolve({ pageId: PAGE_ID }), searchParams: Promise.resolve({ related_offset: "50" }) }),
    );
    expect(api.getPageIntelligence).toHaveBeenCalledWith(PAGE_ID);
    expect(api.listPageRelationships).toHaveBeenCalledWith(PAGE_ID, { limit: 50, offset: 50 });
    expect(api.listSimilarPages).toHaveBeenCalledWith(PAGE_ID, { limit: 10 });
    const section = screen.getByRole("heading", { level: 2, name: "Intelligence" }).closest("section")!;
    const badges = within(section).getByRole("heading", { level: 2 }).parentElement!;
    expect(within(badges).getByText("Protected")).toBeInTheDocument();
    expect(within(section).getByText("Not eligible as target")).toBeInTheDocument();
    expect(within(section).getByRole("list", { name: "Reasons not eligible as target" })).toHaveTextContent(
      "Thin content (below the word-count threshold)",
    );
    expect(within(section).getByRole("table", { name: /related pages/i })).toBeInTheDocument();
    expect(within(section).getByRole("table", { name: /similar pages/i })).toBeInTheDocument();
    // The existing inventory sections are still there.
    expect(screen.getByRole("heading", { name: "Current version (v2)" })).toBeInTheDocument();
  });

  it("falls back to node ids when the taxonomy cannot be loaded", async () => {
    mockDetail();
    api.getTaxonomy.mockResolvedValue(fail({ kind: "network", path: "/intelligence/taxonomy" }));
    await renderPage(PageDetailPage({ params: Promise.resolve({ pageId: PAGE_ID }), searchParams: Promise.resolve({}) }));
    const facts = screen.getByRole("heading", { name: "Classification" }).parentElement!;
    expect(within(facts).getByRole("link", { name: "energy" })).toHaveAttribute("href", "/intelligence/topics/energy");
  });

  it("keeps the inventory page when the intelligence call fails", async () => {
    mockDetail();
    api.getPageIntelligence.mockResolvedValue(fail({ kind: "timeout", path: "/intelligence/pages/{page_id}" }));
    await renderPage(PageDetailPage({ params: Promise.resolve({ pageId: PAGE_ID }), searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("API timed out: Page intelligence unavailable");
    expect(screen.getByRole("heading", { level: 1, name: "Widgets market report" })).toBeInTheDocument();
  });
});

describe("/intelligence", () => {
  it("renders coverage cards, breakdown tables and the taxonomy", async () => {
    api.getIntelligenceCoverage.mockResolvedValue(ok(coverage()));
    await renderPage(IntelligencePage());
    expect(screen.getByRole("heading", { level: 1, name: "Content intelligence" })).toBeInTheDocument();
    expect(screen.getByText("Never analysed").nextElementSibling).toHaveTextContent("50of 1,200");
    expect(screen.getByText("Eligible link targets").nextElementSibling).toHaveTextContent("910");

    const byType = screen.getByRole("table", { name: /pages by page type/i });
    expect(within(byType).getAllByRole("rowheader").map((h) => h.firstChild?.textContent)).toEqual([
      "Market report",
      "unknown_type",
      "Legal page",
    ]);
    const byIndustry = screen.getByRole("table", { name: /pages by industry/i });
    expect(within(byIndustry).getByRole("link", { name: "Healthcare" })).toHaveAttribute(
      "href",
      "/intelligence/topics/healthcare",
    );
    expect(within(byIndustry).getByRole("link", { name: "retired_industry" })).toBeInTheDocument();

    const markets = screen.getByRole("list", { name: "Markets" });
    expect(within(markets).getByRole("link", { name: "EV batteries" })).toHaveAttribute(
      "href",
      "/intelligence/topics/energy.batteries.ev",
    );
    expect(within(screen.getByRole("list", { name: "Geographies" })).getByRole("link", { name: "India" })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /^page types/i })).toBeInTheDocument();
    expect(screen.getByText(/illustrative seed/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Semantic search" })).toHaveAttribute("href", "/intelligence/search");
  });

  it("shows coverage and taxonomy failures independently", async () => {
    api.getIntelligenceCoverage.mockResolvedValue(fail({ kind: "network" }));
    await renderPage(IntelligencePage());
    expect(screen.getByRole("alert")).toHaveTextContent("Intelligence coverage unavailable");
    expect(screen.getByRole("list", { name: "Markets" })).toBeInTheDocument();
  });

  it("still shows coverage when the taxonomy fails", async () => {
    api.getIntelligenceCoverage.mockResolvedValue(ok(coverage()));
    api.getTaxonomy.mockResolvedValue(fail({ kind: "invalid_body", path: "/intelligence/taxonomy" }));
    await renderPage(IntelligencePage());
    expect(screen.getByRole("alert")).toHaveTextContent("Taxonomy unavailable");
    expect(within(screen.getByRole("table", { name: /pages by industry/i })).getByRole("link", { name: "energy" })).toBeInTheDocument();
  });
});

describe("/intelligence/topics/[nodeId]", () => {
  it("lists the node's pages with its label, parent and pagination", async () => {
    api.listTaxonomyNodePages.mockResolvedValue(ok(list([similarPage()], { total: 120, limit: 20, has_more: true })));
    await renderPage(
      TaxonomyNodePage({
        params: Promise.resolve({ nodeId: "energy.batteries.ev" }),
        searchParams: Promise.resolve({ offset: "abc", limit: "20" }),
      }),
    );
    expect(api.listTaxonomyNodePages).toHaveBeenCalledWith("energy.batteries.ev", { limit: 20, offset: 0 });
    expect(screen.getByRole("heading", { level: 1, name: "EV batteries" })).toBeInTheDocument();
    const header = screen.getByRole("heading", { level: 1 }).parentElement!;
    expect(within(header).getByRole("link", { name: "Batteries" })).toHaveAttribute("href", "/intelligence/topics/energy.batteries");
    expect(screen.getByRole("table", { name: /^pages/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /next/i })).toHaveAttribute(
      "href",
      "/intelligence/topics/energy.batteries.ev?limit=20&offset=20",
    );
  });

  it("shows an empty state", async () => {
    api.listTaxonomyNodePages.mockResolvedValue(ok(list([])));
    await renderPage(TaxonomyNodePage({ params: Promise.resolve({ nodeId: "in" }), searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("heading", { level: 1, name: "India" })).toBeInTheDocument();
    expect(screen.getByText("No analysed page lists this node yet.")).toBeInTheDocument();
  });

  it.each([
    ["a 404", NOT_FOUND],
    ["a 422", fail({ kind: "api_error", status: 422, code: "validation_failed" })],
    ["a malformed id", fail({ kind: "invalid_argument" })],
  ])("calls notFound() for %s", async (_label, result) => {
    api.listTaxonomyNodePages.mockResolvedValue(result);
    await expect(
      TaxonomyNodePage({ params: Promise.resolve({ nodeId: "nope" }), searchParams: Promise.resolve({}) }),
    ).rejects.toMatchObject({ digest: expect.stringContaining("404") });
  });

  it("shows an error notice (not a 404) when the API is down", async () => {
    api.listTaxonomyNodePages.mockResolvedValue(fail({ kind: "network" }));
    await renderPage(TaxonomyNodePage({ params: Promise.resolve({ nodeId: "energy" }), searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("API unreachable: Node pages unavailable");
  });
});

describe("/intelligence/search", () => {
  it("prompts for a query and makes no request when q is absent", async () => {
    await renderPage(SemanticSearchPage({ searchParams: Promise.resolve({}) }));
    expect(api.semanticSearch).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("Enter a query");
    const form = screen.getByRole("search", { name: "Semantic search" });
    expect(form).toHaveAttribute("method", "get");
    expect(form).toHaveAttribute("action", "/intelligence/search");
    const input = within(form).getByLabelText("Query");
    expect(input).toHaveAttribute("name", "q");
    expect(input).toHaveAttribute("maxlength", "500");
    expect(input).toHaveAccessibleDescription("2–500 characters.");
    expect(within(form).getByLabelText("Number of results")).toHaveAttribute("name", "limit");
    expect(within(form).getByRole("button", { name: "Search" })).toBeInTheDocument();
  });

  it("explains an invalid query without calling the API", async () => {
    await renderPage(SemanticSearchPage({ searchParams: Promise.resolve({ q: "x" }) }));
    expect(api.semanticSearch).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("The query was not searched. Enter at least 2 characters.");
    expect(screen.getByLabelText("Query")).toHaveValue("x");
  });

  it("searches and renders scored results", async () => {
    api.semanticSearch.mockResolvedValue(ok(list([similarPage(), similarPage({ page_id: PAGE_ID, score: 0.5, title: "Widgets" })])));
    await renderPage(SemanticSearchPage({ searchParams: Promise.resolve({ q: " ev batteries ", limit: "20" }) }));
    expect(api.semanticSearch).toHaveBeenCalledWith({ q: "ev batteries", limit: 20 });
    const table = screen.getByRole("table", { name: /results for “ev batteries”/i });
    expect(within(table).getByRole("columnheader", { name: "Similarity" })).toBeInTheDocument();
    expect(within(table).getByText("0.812")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Widgets" })).toHaveAttribute("href", `/inventory/pages/${PAGE_ID}`);
    expect(within(table).getAllByRole("link", { name: "Batteries" })[0]).toBeInTheDocument();
    expect(screen.getByLabelText("Number of results")).toHaveValue("20");
  });

  it("shows a clear notice, not a crash, when the embedding model is unavailable (503)", async () => {
    api.semanticSearch.mockResolvedValue(MODEL_DOWN);
    await renderPage(SemanticSearchPage({ searchParams: Promise.resolve({ q: "lithium" }) }));
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Semantic search is unavailable: the embedding model cannot be reached");
    expect(alert).not.toHaveTextContent("API rejected the request");
    expect(screen.getByRole("search", { name: "Semantic search" })).toBeInTheDocument();
  });

  it("uses the generic notice for other failures", async () => {
    api.semanticSearch.mockResolvedValue(fail({ kind: "network", path: "/intelligence/search" }));
    await renderPage(SemanticSearchPage({ searchParams: Promise.resolve({ q: "lithium" }) }));
    expect(screen.getByRole("alert")).toHaveTextContent("API unreachable: Semantic search unavailable");
  });

  it("shows an empty result", async () => {
    api.semanticSearch.mockResolvedValue(ok(list([])));
    await renderPage(SemanticSearchPage({ searchParams: Promise.resolve({ q: "lithium" }) }));
    expect(screen.getByText(/No pages matched/)).toBeInTheDocument();
  });
});

describe("/jobs", () => {
  it("passes parsed filters and renders the table", async () => {
    api.listJobRuns.mockResolvedValue(ok(list([jobRunSummary()], { total: 80, has_more: true })));
    await renderPage(JobRunsPage({ searchParams: Promise.resolve({ kind: "analysis", status: "bogus", limit: "0" }) }));
    expect(api.listJobRuns).toHaveBeenCalledWith({ kind: "analysis", status: null, limit: 50, offset: 0 });
    expect(screen.getByRole("table", { name: /job runs/i })).toBeInTheDocument();
    expect(screen.getByLabelText("Kind")).toHaveValue("analysis");
    expect(screen.getByLabelText("Status")).toHaveValue("");
    expect(screen.getByRole("link", { name: /next/i })).toHaveAttribute("href", "/jobs?kind=analysis&offset=50");
  });

  it("shows onboarding, filtered-empty and error states", async () => {
    api.listJobRuns.mockResolvedValueOnce(ok(list([])));
    const first = await renderPage(JobRunsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("status")).toHaveTextContent("No job runs yet — run make analyze");
    first.unmount();

    api.listJobRuns.mockResolvedValueOnce(ok(list([])));
    const second = await renderPage(JobRunsPage({ searchParams: Promise.resolve({ status: "failed" }) }));
    expect(screen.getByRole("status")).toHaveTextContent("No job runs match these filters.");
    second.unmount();

    api.listJobRuns.mockResolvedValueOnce(fail({ kind: "network", path: "/jobs/runs" }));
    await renderPage(JobRunsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("Job runs unavailable");
  });
});

describe("/jobs/[runId]", () => {
  it("renders summary, counters and the config snapshot as text with secrets withheld", async () => {
    api.getJobRun.mockResolvedValue(ok(jobRunDetail({ status: "failed", failure_reason: "Embedding model unreachable." })));
    const { container } = await renderPage(JobRunDetailPage({ params: Promise.resolve({ runId: JOB_RUN_ID }) }));
    expect(api.getJobRun).toHaveBeenCalledWith(JOB_RUN_ID);
    expect(screen.getByRole("heading", { level: 1, name: "Analysis run" })).toBeInTheDocument();
    const header = screen.getByRole("heading", { level: 1 }).parentElement!;
    expect(within(header).getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Embedding model unreachable.")).toBeInTheDocument();
    expect(screen.getByText("42.0 s")).toBeInTheDocument();

    const counters = screen.getByRole("table", { name: /run counters/i });
    expect(within(counters).getAllByRole("rowheader").map((h) => h.firstChild?.textContent)).toEqual([
      "Embeddings written",
      "Pages analyzed",
      "Pages skipped",
    ]);

    const config = screen.getByRole("table", { name: /run configuration/i });
    expect(within(config).getByRole("rowheader", { name: "batch_size" })).toBeInTheDocument();
    expect(within(config).getByText("50")).toBeInTheDocument();
    expect(within(config).getByRole("rowheader", { name: "db_password" }).nextElementSibling).toHaveTextContent("[withheld]");
    expect(container.textContent).not.toContain("should-never-show");
    expect(container.textContent).not.toContain('"nope"');
  });

  it.each([
    ["a 404", NOT_FOUND],
    ["a malformed id", fail({ kind: "invalid_argument" })],
  ])("calls notFound() for %s", async (_label, result) => {
    api.getJobRun.mockResolvedValue(result);
    await expect(JobRunDetailPage({ params: Promise.resolve({ runId: "nope" }) })).rejects.toMatchObject({
      digest: expect.stringContaining("404"),
    });
  });

  it("shows an error notice when the API is down", async () => {
    api.getJobRun.mockResolvedValue(fail({ kind: "timeout", path: "/jobs/runs/{run_id}" }));
    await renderPage(JobRunDetailPage({ params: Promise.resolve({ runId: JOB_RUN_ID }) }));
    expect(screen.getByRole("alert")).toHaveTextContent("API timed out: Job run unavailable");
  });
});
