import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CoverageSummary } from "@/components/intelligence/CoverageSummary";
import { ModelUnavailableNotice } from "@/components/intelligence/ModelUnavailableNotice";
import { PageIntelligencePanel, type PageIntelligencePanelProps } from "@/components/intelligence/PageIntelligencePanel";
import { RelationshipTable } from "@/components/intelligence/RelationshipTable";
import { SimilarPageTable } from "@/components/intelligence/SimilarPageTable";
import { PageTypeTable, TaxonomyTree } from "@/components/intelligence/TaxonomyTree";
import { JobRunTable } from "@/components/jobs/JobRunTable";
import type { ApiResult, PageIntelligenceView } from "@/lib/api-client";
import { taxonomyLabels } from "@/lib/intelligence";

import {
  JOB_RUN_ID,
  analysis,
  coverage,
  embedding,
  jobRunSummary,
  pageIntelligence,
  protection,
  relationship,
  similarPage,
  taxonomy,
} from "./fixtures/intelligence";
import { OTHER_PAGE_ID, PAGE_ID, list } from "./fixtures/inventory";

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, status: 200 };
}

const LABELS = taxonomyLabels(taxonomy());

function renderPanel(view: PageIntelligenceView, overrides: Partial<PageIntelligencePanelProps> = {}) {
  return render(
    <PageIntelligencePanel
      intelligence={ok(view)}
      relationships={ok(list([relationship()], { total: 60, has_more: true }))}
      similar={ok(list([similarPage()]))}
      labels={LABELS}
      pagination={{ pathname: `/inventory/pages/${PAGE_ID}`, params: { version: 1 }, offsetParam: "related_offset" }}
      {...overrides}
    />,
  );
}

describe("PageIntelligencePanel", () => {
  it("renders classification, topics, geographies, entities, keywords, summary and salient blocks", () => {
    renderPanel(pageIntelligence());
    expect(screen.getByRole("heading", { level: 2, name: "Intelligence" })).toBeInTheDocument();
    expect(screen.getByText("Analysis fresh")).toBeInTheDocument();
    expect(screen.getByText("Embedding fresh")).toBeInTheDocument();
    expect(screen.queryByText("Protected")).toBeNull();

    expect(screen.getByText("Commercial research")).toBeInTheDocument();
    expect(screen.getByText("High (3 of 3)")).toBeInTheDocument();
    // Industry and market are shown by label (from the taxonomy) and link to their pages.
    const facts = screen.getByRole("heading", { name: "Classification" }).parentElement!;
    expect(within(facts).getByRole("link", { name: "Energy" })).toHaveAttribute("href", "/intelligence/topics/energy");
    expect(within(facts).getByRole("link", { name: "EV batteries" })).toHaveAttribute(
      "href",
      "/intelligence/topics/energy.batteries.ev",
    );

    const topics = screen.getByRole("table", { name: /^topics/i });
    expect(within(topics).getByText("4.500")).toBeInTheDocument();
    expect(within(topics).getByText("Industry")).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: /^geographies/i })).getByText("Country")).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: /^entities/i })).getByText("Acme Corp")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Keywords" })).getAllByRole("listitem")).toHaveLength(3);
  });

  it("renders page-derived text as text, never as HTML", () => {
    const { container } = renderPanel(pageIntelligence());
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText("The EV battery market in India <b>grows</b> fast.")).toBeInTheDocument();
    expect(screen.getByText("Lithium <script>window.__pwned = true</script> demand")).toBeInTheDocument();
    expect((window as unknown as Record<string, unknown>)["__pwned"]).toBeUndefined();
  });

  it("explains a missing analysis and a missing embedding", () => {
    renderPanel(
      pageIntelligence({
        analysis: null,
        analysis_fresh: false,
        embedding: null,
        embedding_fresh: false,
        eligibility: { as_source: false, as_target: false, source_reasons: ["not_analyzed"], target_reasons: ["not_analyzed"] },
      }),
      { similar: ok(list([])) },
    );
    const header = screen.getByRole("heading", { level: 2, name: "Intelligence" }).parentElement!;
    expect(within(header).getByText("Not analysed")).toBeInTheDocument();
    expect(within(header).getByText("No embedding")).toBeInTheDocument();
    expect(screen.getByText(/has not been analysed yet/)).toHaveTextContent("make analyze");
    expect(screen.queryByRole("table", { name: /^topics/i })).toBeNull();
    expect(screen.getByText("No embedding is stored for this page.")).toBeInTheDocument();
    expect(screen.getByText("No similar pages: this page has no fresh embedding.")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Reasons not eligible as source" })).toHaveTextContent("Not analysed");
  });

  it("flags a stale analysis and a stale embedding", () => {
    renderPanel(
      pageIntelligence({
        analysis: analysis({ analysis_version: "tax0-alg1" }),
        analysis_fresh: false,
        embedding: embedding({ page_version: 1 }),
        embedding_fresh: false,
      }),
    );
    expect(screen.getByText("Analysis stale")).toBeInTheDocument();
    expect(screen.getByText("Embedding stale")).toBeInTheDocument();
    expect(screen.getByText(/This analysis is stale/)).toHaveTextContent("tax0-alg1");
    expect(screen.getByText("(current is v2)", { exact: false })).toBeInTheDocument();
  });

  it("shows active protections before history, with source, rule and reason", () => {
    renderPanel(
      pageIntelligence({
        protections: [
          protection({ id: 1, active: false, source: "cms", rule_id: null, reason: "Old CMS lock." }),
          protection({ id: 2, active: true }),
        ],
      }),
    );
    expect(screen.getByText("Protected")).toBeInTheDocument();
    expect(screen.getByText(/1 active protection blocks/)).toBeInTheDocument();
    const table = screen.getByRole("table", { name: /^protections/i });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[0]!.textContent)).toEqual(["✕Active", "–Inactive"]);
    expect(within(rows[0]!).getByText("Policy rule")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("legal-pages")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("Legal pages are never modified.")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("CMS")).toBeInTheDocument();
  });

  it("renders ineligibility reason codes as human labels", () => {
    renderPanel(
      pageIntelligence({
        eligibility: {
          as_source: false,
          as_target: true,
          // A value from a newer backend, outside this build's contract enum.
          source_reasons: ["thin_content", "noindex", "brand_new_reason" as never],
          target_reasons: [],
        },
      }),
    );
    expect(screen.getByText("Not eligible as source")).toBeInTheDocument();
    expect(screen.getByText("Eligible as target")).toBeInTheDocument();
    const reasons = screen.getByRole("list", { name: "Reasons not eligible as source" });
    expect(within(reasons).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Thin content (below the word-count threshold)",
      "Robots noindex",
      "Unknown (brand_new_reason)",
    ]);
    expect(screen.queryByRole("list", { name: "Reasons not eligible as target" })).toBeNull();
  });

  it("renders relationship counts, related pages with pagination, and similar pages", () => {
    renderPanel(pageIntelligence());
    const counts = screen.getByRole("table", { name: /relationships by type/i });
    expect(within(counts).getAllByRole("rowheader").map((h) => h.firstChild?.textContent)).toEqual([
      "Semantically similar",
      "Same market",
    ]);
    const related = screen.getByRole("table", { name: /related pages/i });
    expect(within(related).getByRole("link", { name: "Gadgets report" })).toHaveAttribute(
      "href",
      `/inventory/pages/${OTHER_PAGE_ID}`,
    );
    expect(within(related).getByText("0.850")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Related pages pagination" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /next/i })).toHaveAttribute(
      "href",
      `/inventory/pages/${PAGE_ID}?version=1&related_offset=50`,
    );
    const similar = screen.getByRole("table", { name: /similar pages/i });
    expect(within(similar).getByText("0.812")).toBeInTheDocument();
    expect(within(similar).getByRole("link", { name: "Batteries" })).toHaveAttribute(
      "href",
      "/intelligence/topics/energy.batteries",
    );
  });

  it("shows error notices without crashing", () => {
    render(
      <PageIntelligencePanel
        intelligence={{ ok: false, error: { kind: "network", path: "/intelligence/pages/{page_id}", message: "Down." } }}
        relationships={ok(list([]))}
        similar={ok(list([]))}
        labels={LABELS}
        pagination={{ pathname: "/x", params: {}, offsetParam: "related_offset" }}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("API unreachable: Page intelligence unavailable");

    renderPanel(pageIntelligence(), {
      relationships: { ok: false, error: { kind: "timeout", path: "/intelligence/pages/{page_id}/relationships", message: "Slow." } },
    });
    expect(screen.getAllByRole("alert").at(-1)).toHaveTextContent("Related pages unavailable");
  });
});

describe("TaxonomyTree", () => {
  it("renders markets as a nested list with page counts and links", () => {
    render(<TaxonomyTree nodes={taxonomy().markets} label="Markets" emptyMessage="None." />);
    const tree = screen.getByRole("list", { name: "Markets" });
    const roots = within(tree).getAllByRole("listitem").filter((li) => li.parentElement === tree);
    expect(roots).toHaveLength(2);
    const energy = roots[0]!;
    expect(within(energy).getByRole("link", { name: "Energy" })).toHaveAttribute("href", "/intelligence/topics/energy");
    expect(energy).toHaveTextContent("4 pages");
    const nested = energy.querySelector("ul ul");
    expect(nested).not.toBeNull();
    expect(within(nested as HTMLElement).getByRole("link", { name: "EV batteries" })).toBeInTheDocument();
    expect(roots[1]).toHaveTextContent("1 page");
  });

  it("shows geography kinds and the empty state", () => {
    const { unmount } = render(<TaxonomyTree nodes={taxonomy().geographies} label="Geographies" showKind emptyMessage="None." />);
    expect(screen.getByText("Region")).toBeInTheDocument();
    unmount();
    render(<TaxonomyTree nodes={[]} label="Markets" emptyMessage="No markets." />);
    expect(screen.getByText("No markets.")).toBeInTheDocument();
  });

  it("renders the page-type table", () => {
    render(<PageTypeTable pageTypes={taxonomy().page_types} />);
    const table = screen.getByRole("table", { name: /page types/i });
    expect(within(table).getByText("Market report")).toBeInTheDocument();
    expect(within(table).getByText("None (0 of 3)")).toBeInTheDocument();
    expect(within(table).getAllByText("No")).toHaveLength(2);
  });
});

describe("CoverageSummary", () => {
  it("renders every coverage count as text with the inventory total", () => {
    render(<CoverageSummary coverage={coverage()} />);
    expect(screen.getByText("Analysed (stale)").nextElementSibling).toHaveTextContent("150of 1,200");
    expect(screen.getByText("Relationships").nextElementSibling).toHaveTextContent("15,432");
    expect(screen.getByText("Relationships").nextElementSibling).not.toHaveTextContent("of");
    expect(screen.getByText("nomic-embed-text")).toBeInTheDocument();
    expect(screen.getByText("tax1-alg1")).toBeInTheDocument();
  });
});

describe("SimilarPageTable and RelationshipTable", () => {
  it("never links a non-http URL and falls back to the URL for an untitled page", () => {
    const { container } = render(
      <SimilarPageTable
        pages={[similarPage({ title: null, canonical_url: "javascript:alert(1)", market_id: "unknown.node" })]}
        caption="Results"
        labels={LABELS}
        emptyMessage="None."
      />,
    );
    for (const anchor of Array.from(container.querySelectorAll("a"))) {
      expect(anchor.getAttribute("href") ?? "").not.toMatch(/^\s*javascript:/i);
    }
    expect(screen.getByRole("link", { name: "javascript:alert(1)" })).toHaveAttribute("href", `/inventory/pages/${OTHER_PAGE_ID}`);
    expect(screen.getByRole("link", { name: "unknown.node" })).toBeInTheDocument();
  });

  it("summarises evidence as text, withholding credential-like keys", () => {
    render(
      <RelationshipTable
        relationships={[relationship({ evidence: { cosine: 0.91, api_token: "secret-value" } })]}
        total={1}
        emptyMessage="None."
      />,
    );
    expect(screen.getByText("Same market")).toBeInTheDocument();
    expect(screen.getByText(/cosine: 0.91/)).toHaveTextContent("api_token: [withheld]");
    expect(screen.queryByText(/secret-value/)).toBeNull();
    expect(screen.getByText("v2 → v1")).toBeInTheDocument();
  });
});

describe("JobRunTable", () => {
  it("lists kind, status, trigger, timings and counters", () => {
    render(<JobRunTable runs={[jobRunSummary({ status: "running", completed_at: null })]} total={3} />);
    const table = screen.getByRole("table", { name: /job runs/i });
    expect(within(table).getByText("(3)")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: /^Analysis/ })).toHaveAttribute("href", `/jobs/${JOB_RUN_ID}`);
    expect(within(table).getByText("Running")).toBeInTheDocument();
    expect(within(table).getByText("cli")).toBeInTheDocument();
    expect(within(table).getByText("In progress")).toBeInTheDocument();
    expect(within(table).getByText("Pages analyzed").nextElementSibling).toHaveTextContent("120");
  });
});

describe("ModelUnavailableNotice", () => {
  it("explains the 503 in plain words as an alert", () => {
    render(
      <ModelUnavailableNotice
        error={{ kind: "api_error", status: 503, code: "model_unavailable", path: "/intelligence/search", message: "x", requestId: "req-9" }}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Semantic search is unavailable: the embedding model cannot be reached");
    expect(alert).toHaveTextContent("model_unavailable");
    expect(alert).toHaveTextContent("req-9");
    expect(within(alert).getByRole("link", { name: "system status" })).toHaveAttribute("href", "/system");
  });
});
