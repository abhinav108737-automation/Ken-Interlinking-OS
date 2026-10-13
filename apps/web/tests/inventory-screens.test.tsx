import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import InventoryPage from "@/app/inventory/page";
import PageDetailPage from "@/app/inventory/pages/[pageId]/page";
import RunDetailPage from "@/app/inventory/runs/[runId]/page";
import RunsPage from "@/app/inventory/runs/page";
import type { ApiError, ApiResult } from "@/lib/api-client";

import {
  OTHER_PAGE_ID,
  PAGE_ID,
  RUN_ID,
  issue,
  link,
  list,
  pageDetail,
  pageSummary,
  runDetail,
  runSummary,
  versionDetail,
  versionSummary,
} from "./fixtures/inventory";

const api = vi.hoisted(() => ({
  listInventoryPages: vi.fn(),
  getInventoryPage: vi.fn(),
  getInventoryPageVersion: vi.fn(),
  listInventoryPageVersions: vi.fn(),
  listInventoryPageLinks: vi.fn(),
  listInventoryLinks: vi.fn(),
  listIngestionRuns: vi.fn(),
  getIngestionRun: vi.fn(),
  listIngestionRunIssues: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, status: 200 };
}

function fail(error: Partial<ApiError> & Pick<ApiError, "kind">): ApiResult<never> {
  return { ok: false, error: { path: "/inventory/pages", message: "It failed.", ...error } };
}

const NOT_FOUND = fail({ kind: "api_error", status: 404, code: "page_not_found", path: "/inventory/pages/{page_id}" });

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
});

async function renderPage(element: Promise<React.ReactElement>) {
  return render(await element);
}

describe("/inventory", () => {
  it("passes parsed filters to the API and renders the table and pagination", async () => {
    api.listInventoryPages.mockResolvedValue(
      ok(list([pageSummary({ gone_at: "2026-10-13T00:00:00Z" })], { total: 120, has_more: true })),
    );
    await renderPage(
      InventoryPage({
        searchParams: Promise.resolve({ q: " widgets ", publication_state: "draft", offset: "abc", limit: "9999" }),
      }),
    );
    expect(api.listInventoryPages).toHaveBeenCalledWith({
      q: "widgets",
      publication_state: "draft",
      limit: 200,
      offset: 0,
    });
    expect(screen.getByRole("heading", { level: 1, name: "Content inventory" })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /pages/i })).toBeInTheDocument();
    expect(screen.getByText("Gone")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /next/i })).toHaveAttribute(
      "href",
      "/inventory?q=widgets&publication_state=draft&limit=200&offset=50",
    );
  });

  it("uses a labelled GET search form, not a mutation", async () => {
    api.listInventoryPages.mockResolvedValue(ok(list([pageSummary()])));
    await renderPage(InventoryPage({ searchParams: Promise.resolve({ publication_state: "bogus" }) }));
    expect(api.listInventoryPages).toHaveBeenCalledWith(expect.objectContaining({ publication_state: null, q: null }));
    const form = screen.getByRole("search", { name: "Filter pages" });
    expect(form).toHaveAttribute("method", "get");
    expect(form).toHaveAttribute("action", "/inventory");
    expect(within(form).getByLabelText("Search title or URL")).toHaveAttribute("name", "q");
    expect(within(form).getByLabelText("Publication state")).toHaveAttribute("name", "publication_state");
    expect(within(form).queryByRole("link", { name: /clear/i })).toBeNull();
  });

  it("shows the onboarding empty state when nothing has been ingested", async () => {
    api.listInventoryPages.mockResolvedValue(ok(list([])));
    await renderPage(InventoryPage({ searchParams: Promise.resolve({}) }));
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("No pages yet");
    expect(status).toHaveTextContent("make ingest-file FILE=…");
    expect(status).toHaveTextContent("KEN_CRAWL_BASE_URL");
    expect(status).toHaveTextContent("make crawl");
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("distinguishes an empty filter result from an empty inventory", async () => {
    api.listInventoryPages.mockResolvedValue(ok(list([])));
    await renderPage(InventoryPage({ searchParams: Promise.resolve({ q: "zzz" }) }));
    expect(screen.getByRole("status")).toHaveTextContent("No pages match these filters.");
    expect(screen.getByRole("link", { name: /clear filters/i })).toHaveAttribute("href", "/inventory");
  });

  it("renders the API error notice", async () => {
    api.listInventoryPages.mockResolvedValue(fail({ kind: "network" }));
    await renderPage(InventoryPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("API unreachable: Page inventory unavailable");
  });
});

function mockDetail() {
  api.getInventoryPage.mockResolvedValue(ok(pageDetail()));
  api.listInventoryPageVersions.mockResolvedValue(
    ok(list([versionSummary(), versionSummary({ version: 1, content_hash: "sha256:ffff" })])),
  );
  api.listInventoryPageLinks.mockResolvedValue(
    ok(
      list([
        link(),
        link({
          ordinal: 1,
          href: "javascript:alert(1)",
          link_type: "non_http",
          resolution: "not_applicable",
          resolved_url: null,
          target_page_id: null,
        }),
      ]),
    ),
  );
  api.listInventoryLinks.mockResolvedValue(
    ok(list([link({ page_id: OTHER_PAGE_ID, source_url: "https://www.example.com/reports/gadgets", target_page_id: PAGE_ID })])),
  );
}

describe("/inventory/pages/[pageId]", () => {
  it("renders identity, version facts, headings, metadata, body text, versions and links", async () => {
    mockDetail();
    const { container } = await renderPage(
      PageDetailPage({ params: Promise.resolve({ pageId: PAGE_ID }), searchParams: Promise.resolve({}) }),
    );
    expect(api.getInventoryPageVersion).not.toHaveBeenCalled();
    expect(api.listInventoryPageLinks).toHaveBeenCalledWith(PAGE_ID, {
      version: null,
      link_type: null,
      limit: 50,
      offset: 0,
    });
    expect(api.listInventoryLinks).toHaveBeenCalledWith({ target_page_id: PAGE_ID, limit: 50, offset: 0 });

    expect(screen.getByRole("heading", { level: 1, name: "Widgets market report" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Current version (v2)" })).toBeInTheDocument();
    expect(screen.getByText(/^Self/)).toBeInTheDocument();
    for (const hash of screen.getAllByText("0123456789ab")) {
      expect(hash).toHaveAttribute("title", "sha256:0123456789abcdef0123456789abcdef");
    }
    expect(screen.getByText("index, follow")).toBeInTheDocument();
    expect(screen.getByText("Market size")).toBeInTheDocument();
    // Metadata is text; a credential-looking key is withheld.
    expect(screen.getByRole("rowheader", { name: "og:title" })).toBeInTheDocument();
    expect(screen.queryByText("abc123")).toBeNull();
    // Canonical URL is an external link with safe attributes.
    const canonical = screen.getAllByRole("link", { name: /^https:\/\/www\.example\.com\/reports\/widgets/ })[0]!;
    expect(canonical).toHaveAttribute("rel", "noopener noreferrer nofollow");
    // The run that produced the version is linked internally.
    expect(screen.getByRole("link", { name: RUN_ID })).toHaveAttribute("href", `/inventory/runs/${RUN_ID}`);
    // Version history links to older versions by query string.
    expect(screen.getByRole("link", { name: /v1/ })).toHaveAttribute("href", `/inventory/pages/${PAGE_ID}?version=1`);
    // Body links and inbound links.
    expect(screen.getByRole("table", { name: /body links/i })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /inbound links/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://www.example.com/reports/gadgets" })).toHaveAttribute(
      "href",
      `/inventory/pages/${OTHER_PAGE_ID}`,
    );
    for (const anchor of Array.from(container.querySelectorAll("a"))) {
      expect(anchor.getAttribute("href") ?? "").not.toMatch(/^\s*javascript:/i);
    }
  });

  it("never renders body_html as HTML", async () => {
    mockDetail();
    const { container } = await renderPage(
      PageDetailPage({ params: Promise.resolve({ pageId: PAGE_ID }), searchParams: Promise.resolve({}) }),
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.innerHTML).not.toContain("<script");
    expect(container.innerHTML).not.toContain("onerror");
    expect((window as unknown as Record<string, unknown>)["__pwned"]).toBeUndefined();
    expect(screen.getByRole("region", { name: "Body text of version 2" })).toHaveTextContent(
      "Second paragraph of the widgets report.",
    );
  });

  it("inspects an older version's text and links via ?version=N", async () => {
    mockDetail();
    api.getInventoryPageVersion.mockResolvedValue(
      ok(versionDetail({ version: 1, body_text: "Old body text.", content_hash: "sha256:ffff" })),
    );
    await renderPage(
      PageDetailPage({
        params: Promise.resolve({ pageId: PAGE_ID }),
        searchParams: Promise.resolve({ version: "1", link_type: "internal", links_offset: "50" }),
      }),
    );
    expect(api.getInventoryPageVersion).toHaveBeenCalledWith(PAGE_ID, 1);
    expect(api.listInventoryPageLinks).toHaveBeenCalledWith(PAGE_ID, {
      version: 1,
      link_type: "internal",
      limit: 50,
      offset: 50,
    });
    expect(screen.getByRole("heading", { name: "Version 1 (not current)" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Body text of version 1" })).toHaveTextContent("Old body text.");
    expect(screen.getByRole("link", { name: /show the current version/i })).toHaveAttribute(
      "href",
      `/inventory/pages/${PAGE_ID}`,
    );
    const form = screen.getByRole("search", { name: "Filter links" });
    expect(form.querySelector('input[type="hidden"][name="version"]')).toHaveAttribute("value", "1");
    expect(within(form).getByLabelText("Link type")).toHaveValue("internal");
  });

  it("falls back to the current version when the requested one is missing", async () => {
    mockDetail();
    api.getInventoryPageVersion.mockResolvedValue(NOT_FOUND);
    await renderPage(
      PageDetailPage({ params: Promise.resolve({ pageId: PAGE_ID }), searchParams: Promise.resolve({ version: "9" }) }),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Version 9 unavailable");
    expect(screen.getByRole("heading", { name: "Current version (v2)" })).toBeInTheDocument();
  });

  it.each([
    ["a 404", NOT_FOUND],
    ["a 422", fail({ kind: "api_error", status: 422, code: "validation_failed" })],
    ["a malformed id", fail({ kind: "invalid_argument" })],
  ])("calls notFound() for %s", async (_label, result) => {
    mockDetail();
    api.getInventoryPage.mockResolvedValue(result);
    await expect(
      PageDetailPage({ params: Promise.resolve({ pageId: "nope" }), searchParams: Promise.resolve({}) }),
    ).rejects.toMatchObject({ digest: expect.stringContaining("404") });
  });

  it("shows an error notice (not a 404) when the API is down", async () => {
    mockDetail();
    api.getInventoryPage.mockResolvedValue(fail({ kind: "timeout" }));
    await renderPage(PageDetailPage({ params: Promise.resolve({ pageId: PAGE_ID }), searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("API timed out: Page unavailable");
  });
});

describe("/inventory/runs", () => {
  it("passes filters and renders runs", async () => {
    api.listIngestionRuns.mockResolvedValue(ok(list([runSummary()])));
    await renderPage(RunsPage({ searchParams: Promise.resolve({ source_type: "cms", status: "nope" }) }));
    expect(api.listIngestionRuns).toHaveBeenCalledWith({ source_type: "cms", status: null, limit: 50, offset: 0 });
    expect(screen.getByRole("table", { name: /ingestion runs/i })).toBeInTheDocument();
    expect(screen.getByLabelText("Source type")).toHaveValue("cms");
    expect(screen.getByLabelText("Status")).toHaveValue("");
  });

  it("shows empty and error states", async () => {
    api.listIngestionRuns.mockResolvedValueOnce(ok(list([])));
    const { unmount } = await renderPage(RunsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("status")).toHaveTextContent("No runs yet");
    unmount();
    api.listIngestionRuns.mockResolvedValueOnce(fail({ kind: "invalid_body", path: "/inventory/runs" }));
    await renderPage(RunsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("Ingestion runs unavailable");
  });
});

describe("/inventory/runs/[runId]", () => {
  it("renders counters, config snapshot as text, issue counts and issues", async () => {
    api.getIngestionRun.mockResolvedValue(ok(runDetail({ status: "failed", failure_reason: "Source unreachable." })));
    api.listIngestionRunIssues.mockResolvedValue(ok(list([issue()], { total: 60, has_more: true })));
    const { container } = await renderPage(
      RunDetailPage({ params: Promise.resolve({ runId: RUN_ID }), searchParams: Promise.resolve({ severity: "error" }) }),
    );
    expect(api.listIngestionRunIssues).toHaveBeenCalledWith(RUN_ID, { severity: "error", limit: 50, offset: 0 });
    const header = screen.getByRole("heading", { level: 1 }).parentElement!;
    expect(within(header).getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Source unreachable.")).toBeInTheDocument();

    const config = screen.getByRole("table", { name: /run configuration/i });
    expect(within(config).getByRole("rowheader", { name: "max_pages" })).toBeInTheDocument();
    expect(within(config).getByText("500")).toBeInTheDocument();
    expect(within(config).getByText('["www.example.com"]')).toBeInTheDocument();
    expect(within(config).getByText("true")).toBeInTheDocument();
    expect(within(config).getByText("https://www.example.com/")).toBeInTheDocument();
    expect(container.textContent).not.toContain("should-never-show");
    expect(container.textContent).not.toContain('"nope"');

    const counts = screen.getByRole("table", { name: /issue counts/i });
    expect(within(counts).getAllByRole("rowheader").map((h) => h.textContent)).toEqual([
      "robots_disallowed",
      "http_not_found",
    ]);

    const issues = screen.getByRole("table", { name: /^issues/i });
    expect(within(issues).queryByRole("link")).toBeNull();
    expect(screen.getByRole("link", { name: /next/i })).toHaveAttribute(
      "href",
      `/inventory/runs/${RUN_ID}?severity=error&offset=50`,
    );
    expect(screen.getByLabelText("Severity")).toHaveValue("error");
  });

  it("calls notFound() for an unknown run", async () => {
    api.getIngestionRun.mockResolvedValue(NOT_FOUND);
    api.listIngestionRunIssues.mockResolvedValue(NOT_FOUND);
    await expect(
      RunDetailPage({ params: Promise.resolve({ runId: RUN_ID }), searchParams: Promise.resolve({}) }),
    ).rejects.toMatchObject({ digest: expect.stringContaining("404") });
  });

  it("keeps the run visible when only the issues request fails", async () => {
    api.getIngestionRun.mockResolvedValue(ok(runDetail({ issue_counts: {} })));
    api.listIngestionRunIssues.mockResolvedValue(fail({ kind: "network", path: "/inventory/runs/{run_id}/issues" }));
    await renderPage(RunDetailPage({ params: Promise.resolve({ runId: RUN_ID }), searchParams: Promise.resolve({}) }));
    expect(screen.getByText("This run recorded no issues.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Run issues unavailable");
  });
});
