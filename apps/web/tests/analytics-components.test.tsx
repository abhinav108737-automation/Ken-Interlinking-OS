import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AttributionChain } from "@/components/analytics/AttributionChain";
import { BarPair, barPairSummary } from "@/components/analytics/BarPair";
import { ConfounderChips } from "@/components/analytics/ConfounderChips";
import { DailyTotalsChart } from "@/components/analytics/DailyTotalsChart";
import { ImportsTable } from "@/components/analytics/ImportsTable";
import { KpiTiles } from "@/components/analytics/KpiTiles";
import { LinkPerformanceTable } from "@/components/analytics/LinkPerformanceTable";
import { NoAnalyticsData } from "@/components/analytics/NoAnalyticsData";
import { ObservedChangeNote } from "@/components/analytics/ObservedChangeNote";
import { PageSeriesPanel } from "@/components/analytics/PageSeriesPanel";
import { Sparkline, sparklineSummary } from "@/components/analytics/Sparkline";
import { SourceFreshnessTable } from "@/components/analytics/SourceFreshnessTable";
import { SyntheticBadge, SyntheticBanner } from "@/components/analytics/SyntheticNotice";
import { WindowComparisonCard } from "@/components/analytics/WindowComparisonCard";
import { SYNTHETIC_BADGE_LABEL } from "@/lib/analytics";

import {
  analyticsImportView,
  linkAttribution,
  linkPerformanceView,
  metricTotals,
  pageDailyPoints,
  pageWindowComparison,
  sourceFreshness,
  windowStats,
} from "./fixtures/analytics";
import { PLAN_ID } from "./fixtures/recommendations";

const XSS = '<img src=x onerror="alert(1)">';

const POINTS = [
  { date: "2026-09-01", value: 3 },
  { date: "2026-09-02", value: 9 },
  { date: "2026-09-03", value: 1 },
];

describe("Sparkline", () => {
  it("is an image with a title that states the data, and keeps the numbers in the table", () => {
    const { container } = render(<Sparkline id="s" points={POINTS} label="Clicks per day" />);
    const svg = container.querySelector("svg")!;
    expect(svg).toHaveAttribute("role", "img");
    const title = container.querySelector("title")!;
    expect(svg).toHaveAttribute("aria-labelledby", title.id);
    expect(title.textContent).toContain("Clicks per day: 3 days from 2026-09-01 to 2026-09-03");
    expect(title.textContent).toContain("highest 9 on 2026-09-02");
    expect(title.textContent).toContain("table below");
    // One vertex per day.
    expect(container.querySelector("polyline")!.getAttribute("points")!.split(" ")).toHaveLength(3);
    expect(container.querySelector("script")).toBeNull();
  });

  it("draws a single day as a point and renders nothing without data", () => {
    const one = render(<Sparkline id="s" points={[POINTS[0]!]} label="Clicks per day" />);
    expect(one.container.querySelector("polyline")).toBeNull();
    expect(one.container.querySelectorAll("circle")).toHaveLength(1);
    one.unmount();
    const none = render(<Sparkline id="s" points={[]} label="Clicks per day" />);
    expect(none.container.querySelector("svg")).toBeNull();
    expect(sparklineSummary([], "Clicks per day")).toBe("Clicks per day: no data.");
  });

  it("draws an all-zero series flat instead of dividing by zero", () => {
    const { container } = render(
      <Sparkline id="s" points={POINTS.map((point) => ({ ...point, value: 0 }))} label="Clicks per day" />,
    );
    const ys = container
      .querySelector("polyline")!
      .getAttribute("points")!
      .split(" ")
      .map((pair) => pair.split(",")[1]);
    expect(new Set(ys).size).toBe(1);
  });
});

describe("BarPair", () => {
  it("titles itself with both means and the non-causal wording", () => {
    const { container } = render(<BarPair id="b" pre={5} post={7} label="Clicks per day" />);
    expect(container.querySelector("svg")).toHaveAttribute("role", "img");
    expect(container.querySelector("title")!.textContent).toBe(
      "Clicks per day: before 5.0, after 7.0. This is an observed change, not a causal effect. The exact values are in the table below.",
    );
    expect(container.querySelectorAll("rect")).toHaveLength(2);
  });

  it("draws no bar for a window with no mean", () => {
    const { container } = render(<BarPair id="b" pre={null} post={7} label="Clicks per day" />);
    expect(container.querySelectorAll("rect")).toHaveLength(1);
    expect(barPairSummary(null, null, "Clicks per day")).toContain("before —, after —");
  });
});

describe("DailyTotalsChart", () => {
  it("renders two sparklines and the exact values as a table", () => {
    render(
      <DailyTotalsChart
        id="d"
        points={[
          { date: "2026-09-01", impressions: 100, clicks: 3 },
          { date: "2026-09-02", impressions: 110, clicks: 4 },
        ]}
        scopeLabel="all sources"
        caption="Link impressions and clicks per day"
      />,
    );
    expect(screen.getByRole("img", { name: /Impressions per day: 2 days/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Clicks per day: 2 days/ })).toBeInTheDocument();
    const table = screen.getByRole("table", { name: /daily values/ });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(table).toHaveTextContent("2026-09-02");
    expect(table).toHaveTextContent("110");
  });

  it("says so when no day is stored", () => {
    render(<DailyTotalsChart id="d" points={[]} scopeLabel="GA4" caption="Link impressions and clicks per day" />);
    expect(screen.getByRole("status")).toHaveTextContent("No daily rows are stored for this range (GA4).");
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("KpiTiles", () => {
  it("shows four figures and an em dash for a CTR without impressions", () => {
    render(
      <KpiTiles
        liveLinks={7}
        linksWithData={4}
        totals={metricTotals({ impressions: 0, clicks: 0, ctr: null })}
        scopeLabel="all sources"
      />,
    );
    const group = screen.getByLabelText("Key figures");
    expect(group).toHaveTextContent("Live links");
    expect(group).toHaveTextContent("Impressions");
    expect(group).toHaveTextContent("Clicks");
    expect(group).toHaveTextContent("CTR");
    // Landings were cut from Phase 7.
    expect(group).not.toHaveTextContent(/landing/i);
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByText(/No impressions in range/)).toBeInTheDocument();
  });
});

describe("ConfounderChips", () => {
  it("names each flag, with its explanation as a title", () => {
    render(<ConfounderChips flags={["content_changed", "synthetic_data", "solar_flare"]} />);
    const list = screen.getByRole("list", { name: "Confounders" });
    expect(within(list).getByText("Content changed")).toHaveAttribute("title", expect.stringContaining("new version"));
    expect(within(list).getByText("Synthetic data")).toBeInTheDocument();
    expect(within(list).getByText("Unknown (solar_flare)")).toBeInTheDocument();
  });

  it("does not claim the absence of a flag means the link caused anything", () => {
    render(<ConfounderChips flags={[]} />);
    expect(screen.getByText("No confounder was flagged. Other causes are still possible.")).toBeInTheDocument();
  });
});

describe("the synthetic markers", () => {
  it("warns persistently and badges each row", () => {
    const banner = render(<SyntheticBanner />);
    expect(screen.getByRole("alert")).toHaveTextContent("Synthetic demo data");
    expect(screen.getByRole("alert")).toHaveTextContent("make analytics-demo-seed");
    expect(screen.getByRole("alert")).toHaveTextContent("never moves a recommendation to “measured”");
    // Not dismissible: there is no control on it at all.
    expect(screen.queryByRole("button")).toBeNull();
    banner.unmount();
    const badge = render(<SyntheticBadge synthetic />);
    expect(screen.getByText(SYNTHETIC_BADGE_LABEL)).toBeInTheDocument();
    badge.unmount();
    const none = render(<SyntheticBadge synthetic={false} />);
    expect(none.container).toBeEmptyDOMElement();
    const absent = render(<SyntheticBadge synthetic={undefined} />);
    expect(absent.container).toBeEmptyDOMElement();
  });
});

describe("ObservedChangeNote", () => {
  it("is a prominent, fixed note that refuses every causal claim", () => {
    render(<ObservedChangeNote />);
    const note = screen.getByRole("note", { name: "Observed change, not a causal effect" });
    expect(note).toHaveTextContent("not a causal effect");
    expect(note).toHaveTextContent("statistical significance");
    expect(note).toHaveTextContent("Seasonality");
  });
});

describe("NoAnalyticsData", () => {
  it("shows the exact commands, and offers no button that could fetch", () => {
    const { container } = render(<NoAnalyticsData />);
    expect(screen.getByRole("status")).toHaveTextContent("No analytics data yet");
    const text = container.textContent ?? "";
    expect(text).toContain("make analytics-import SOURCE=first_party DATASET=links START=2026-09-01 END=2026-09-28");
    expect(text).toContain("make analytics-aggregate");
    expect(text).toContain("make analytics-demo-seed");
    expect(container.querySelector("button")).toBeNull();
    expect(container.querySelector("form")).toBeNull();
  });
});

describe("LinkPerformanceTable", () => {
  it("shows totals, a status, the live window and a plan link", () => {
    render(
      <LinkPerformanceTable
        links={[linkPerformanceView()]}
        caption="Most clicked links (all sources)"
        emptyMessage="No link has a stored row in this range."
      />,
    );
    const table = screen.getByRole("table", { name: /Most clicked links/ });
    expect(within(table).getByRole("link", { name: /view recommendation/ })).toHaveAttribute(
      "href",
      `/recommendations/${PLAN_ID}`,
    );
    expect(table).toHaveTextContent("1,000");
    expect(table).toHaveTextContent("2.50%");
    expect(table).toHaveTextContent("First-party");
    expect(table).toHaveTextContent("Live since 2026-09-01");
  });

  it("says a live link has no events, and never shows 0 % for no impressions", () => {
    render(
      <LinkPerformanceTable
        links={[linkPerformanceView({ totals: { first_party: metricTotals({ impressions: 0, clicks: 0, ctr: null }) } })]}
        caption="Links"
        emptyMessage="none"
      />,
    );
    const table = screen.getByRole("table", { name: /^Links/ });
    expect(table).toHaveTextContent("Live since 2026-09-01 — no events recorded");
    expect(table).toHaveTextContent("—");
    expect(table).not.toHaveTextContent("0.00%");
  });

  it("says a rolled-back link's window has closed", () => {
    render(
      <LinkPerformanceTable
        links={[linkPerformanceView({ plan_status: "rolled_back", rolled_back_at: "2026-09-10T08:00:00Z" })]}
        caption="Links"
        emptyMessage="none"
      />,
    );
    expect(screen.getByRole("table", { name: /^Links/ })).toHaveTextContent(
      "Window closed: rolled back 2026-09-10. No data is collected for it after that.",
    );
  });

  it("badges a synthetic row and renders model text as text", () => {
    const { container } = render(
      <LinkPerformanceTable
        links={[linkPerformanceView({ synthetic: true, anchor_text: XSS, target_url: "javascript:alert(1)" })]}
        caption="Links"
        emptyMessage="none"
      />,
    );
    const table = screen.getByRole("table", { name: /^Links/ });
    expect(within(table).getByText(SYNTHETIC_BADGE_LABEL)).toBeInTheDocument();
    expect(table).toHaveTextContent(XSS);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
  });

  it("has an explicit empty row", () => {
    render(<LinkPerformanceTable links={[]} caption="Links" emptyMessage="No analytics row is stored for these links yet." />);
    expect(screen.getByRole("table", { name: /^Links/ })).toHaveTextContent("No analytics row is stored for these links yet.");
  });
});

describe("SourceFreshnessTable", () => {
  it("names a source that was never imported instead of hiding it", () => {
    render(
      <SourceFreshnessTable
        sources={[
          sourceFreshness(),
          sourceFreshness({ source: "ga4", latest_date: null, last_imported_at: null, last_via: null, last_import_id: null }),
        ]}
      />,
    );
    const table = screen.getByRole("table", { name: /Source freshness/ });
    expect(table).toHaveTextContent("Never imported");
    expect(table).toHaveTextContent("First-party collector");
    expect(table).toHaveTextContent("2026-09-28");
  });

  it("has an empty state", () => {
    render(<SourceFreshnessTable sources={[]} />);
    expect(screen.getByRole("table", { name: /Source freshness/ })).toHaveTextContent("No source has been imported yet.");
  });
});

describe("ImportsTable", () => {
  it("shows the ledger row with its source, days, rows and file", () => {
    render(
      <ImportsTable
        imports={[
          analyticsImportView({ via: "csv", file_name: XSS, file_sha256: "a".repeat(64), sampled: true, synthetic: true }),
        ]}
      />,
    );
    const table = screen.getByRole("table", { name: /Recent imports/ });
    expect(table).toHaveTextContent("CSV file");
    expect(table).toHaveTextContent("812");
    expect(table).toHaveTextContent("Days are UTC days");
    expect(table).toHaveTextContent("(sampled)");
    expect(within(table).getByText(SYNTHETIC_BADGE_LABEL)).toBeInTheDocument();
    // A provider-supplied file name is text, never markup.
    expect(table).toHaveTextContent(XSS);
    expect(table.querySelector("img")).toBeNull();
  });

  it("explains that a failed import leaves no row", () => {
    render(<ImportsTable imports={[]} />);
    expect(screen.getByRole("table", { name: /Recent imports/ })).toHaveTextContent("No import has succeeded yet.");
  });
});

describe("WindowComparisonCard", () => {
  it("shows both windows, the change, the site trend and the confounders", () => {
    render(<WindowComparisonCard id="c" comparison={pageWindowComparison()} />);
    const card = screen.getByRole("article", { name: /Search Console: Clicks around 2026-09-01/ });
    expect(card).toHaveTextContent("Target page (the link points here)");
    expect(card).toHaveTextContent("+2.0");
    expect(card).toHaveTextContent("1.40×");
    expect(card).toHaveTextContent("Site trend (context only)");
    expect(card).toHaveTextContent("1.05×");
    expect(within(card).getByRole("img", { name: /observed change, not a causal effect/ })).toBeInTheDocument();
    expect(within(card).getByRole("list", { name: /Confounders/ })).toHaveTextContent("Content changed");
    // Search Console is compared on clicks and also reports position.
    const table = within(card).getByRole("table", { name: /Before and after/ });
    expect(table).toHaveTextContent("Average position");
    expect(table).toHaveTextContent("12.4");
    expect(table).toHaveTextContent("Days with data");
    expect(within(card).getByRole("link", { name: "View the recommendation" })).toHaveAttribute(
      "href",
      `/recommendations/${PLAN_ID}`,
    );
  });

  it("refuses to show a change when a window has too little data, and says how little", () => {
    render(
      <WindowComparisonCard
        id="c"
        comparison={pageWindowComparison({
          pre: windowStats({ covered_days: 9, mean_per_day: null, clicks: 20 }),
          delta_per_day: null,
          ratio: null,
          flags: ["insufficient_data"],
        })}
      />,
    );
    const card = screen.getByRole("article");
    expect(within(card).getByRole("status")).toHaveTextContent("Pre-window has 9 of 28 days");
    expect(card).toHaveTextContent("Not shown");
    expect(card).toHaveTextContent("Not enough data");
  });

  it("compares a GA4 page on pageviews and reports sessions", () => {
    render(
      <WindowComparisonCard
        id="c"
        comparison={pageWindowComparison({ source: "ga4", metric: "pageviews", role: "source", flags: [] })}
        linkToPlan={false}
      />,
    );
    const card = screen.getByRole("article", { name: /GA4: Pageviews/ });
    expect(card).toHaveTextContent("Source page (the link was added here)");
    expect(within(card).getByRole("table", { name: /Before and after/ })).toHaveTextContent("Sessions (total)");
    expect(within(card).queryByRole("link", { name: "View the recommendation" })).toBeNull();
    expect(card).toHaveTextContent("No confounder was flagged");
  });
});

describe("heading levels", () => {
  it("lets a card sit under an h3 section as an h4", () => {
    const three = render(<WindowComparisonCard id="c" comparison={pageWindowComparison()} />);
    expect(screen.getByRole("heading", { level: 3, name: /Search Console: Clicks/ })).toBeInTheDocument();
    three.unmount();
    render(<WindowComparisonCard id="c" comparison={pageWindowComparison()} headingLevel={4} />);
    expect(screen.getByRole("heading", { level: 4, name: /Search Console: Clicks/ })).toBeInTheDocument();
  });
});

describe("PageSeriesPanel", () => {
  it("charts clicks for Search Console and pageviews for GA4", () => {
    const search = render(<PageSeriesPanel id="p" source="search_console" points={pageDailyPoints(3)} />);
    expect(screen.getByRole("img", { name: /Search Console clicks per day/ })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /Search Console daily rows/ })).toHaveTextContent("11.5");
    search.unmount();
    render(<PageSeriesPanel id="p" source="ga4" points={pageDailyPoints(2)} />);
    expect(screen.getByRole("img", { name: /GA4 pageviews per day/ })).toBeInTheDocument();
  });

  it("says when a source has no stored row", () => {
    render(<PageSeriesPanel id="p" source="ga4" points={[]} />);
    expect(screen.getByRole("status")).toHaveTextContent("No GA4 rows are stored for this window.");
  });
});

describe("AttributionChain", () => {
  it("shows the chain from run to rollback, and names each absent step", () => {
    render(
      <AttributionChain
        attribution={linkAttribution({
          agent_run_id: null,
          agent_verdict: null,
          agent_model: null,
          agent_graph_version: null,
          approved_by: null,
          approved_at: null,
          deployment_id: null,
          supersedes: null,
          calibration_version: null,
        })}
      />,
    );
    const chain = screen.getByRole("list", { name: "Attribution chain" });
    expect(chain).toHaveTextContent("No agent reviewed this link");
    expect(chain).toHaveTextContent("No approval recorded");
    expect(chain).toHaveTextContent("Never deployed");
    expect(chain).toHaveTextContent("Not rolled back");
    expect(chain).toHaveTextContent("Nothing (this is the original recommendation)");
    expect(screen.getAllByRole("link", { name: "Page impact" })[0]).toHaveAttribute(
      "href",
      expect.stringContaining("/analytics/pages/"),
    );
  });

  it("links the agent run, deployment and superseded plan when they exist", () => {
    render(<AttributionChain attribution={linkAttribution()} />);
    const chain = screen.getByRole("list", { name: "Attribution chain" });
    expect(chain).toHaveTextContent("accept");
    expect(chain).toHaveTextContent("qwen2.5:14b");
    expect(chain).toHaveTextContent("olivia");
    expect(within(chain).getByRole("link", { name: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" })).toHaveAttribute(
      "href",
      "/deployments/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    );
    expect(screen.getByText(/engine/)).toHaveTextContent("rec-1.2.0");
  });
});
