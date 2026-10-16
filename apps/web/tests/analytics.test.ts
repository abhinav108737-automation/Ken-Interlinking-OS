import { describe, expect, it } from "vitest";

import type { AnalyticsSource, ConfounderFlag } from "@/lib/api-client";
import {
  ANALYTICS_SOURCES,
  CONFOUNDER_FLAGS,
  DATASETS,
  FIRST_PARTY_CAVEAT,
  IMPORT_COMMANDS,
  IMPORT_VIAS,
  LINK_SORTS,
  MAX_RANGE_DAYS,
  MIN_COVERED_DAYS,
  OBSERVED_CHANGE_NOTE,
  SOURCE_SUM_CAVEAT,
  SYNTHETIC_BANNER_NOTE,
  combineTotals,
  comparisonCoverage,
  coverageNote,
  daySpan,
  describeComparisonMetric,
  describeComparisonRole,
  describeConfounderFlag,
  describeDataset,
  describeImportVia,
  describeLinkSort,
  describeLiveWindow,
  describeRange,
  describeSource,
  foldDailyTotals,
  formatCtr,
  formatDelta,
  formatMean,
  formatPosition,
  formatRatio,
  hasNoEvents,
  hasSynthetic,
  isAnalyticsSource,
  isIsoDate,
  linkSeriesPoints,
  noEventsNote,
  pageImpactHref,
  parseRange,
  parseWindowDays,
  rangeQuery,
  shiftDay,
  sourcesOf,
  totalsFor,
  wasEverApplied,
  windowTotal,
} from "@/lib/analytics";
import { NAV_LINKS, activeNavHref, navLinksFor } from "@/lib/nav";

import { dailyTotals, linkDailyPoints, metricTotals, pageWindowComparison, windowStats } from "./fixtures/analytics";

describe("labels", () => {
  it("labels every contract value and marks an unknown one", () => {
    for (const source of ANALYTICS_SOURCES) expect(describeSource(source)).not.toMatch(/^Unknown/);
    for (const dataset of DATASETS) expect(describeDataset(dataset)).not.toMatch(/^Unknown/);
    for (const via of IMPORT_VIAS) expect(describeImportVia(via)).not.toMatch(/^Unknown/);
    for (const sort of LINK_SORTS) expect(describeLinkSort(sort)).not.toMatch(/^Unknown/);
    expect(describeSource("bigquery")).toBe("Unknown (bigquery)");
    expect(describeSource("")).toBe("Unknown (empty)");
    expect(describeDataset("queries")).toBe("Unknown (queries)");
    expect(describeComparisonMetric("clicks")).toBe("Clicks");
    expect(describeComparisonMetric("pageviews")).toBe("Pageviews");
    expect(describeComparisonRole("source")).toMatch(/Source page/);
    expect(describeComparisonRole("target")).toMatch(/Target page/);
  });

  it("presents every confounder flag, and degrades an unknown one", () => {
    for (const flag of CONFOUNDER_FLAGS) {
      const presentation = describeConfounderFlag(flag);
      expect(presentation.known).toBe(true);
      expect(presentation.label).not.toMatch(/^Unknown/);
      expect(presentation.description.length).toBeGreaterThan(10);
    }
    expect(describeConfounderFlag("solar_flare")).toMatchObject({ known: false, label: "Unknown (solar_flare)" });
    // Synthetic data is the loudest flag: it says the numbers are made up.
    expect(describeConfounderFlag("synthetic_data").description).toMatch(/made up/);
    expect(describeConfounderFlag("insufficient_data").description).toContain(String(MIN_COVERED_DAYS));
  });

  it("knows which values are analytics sources", () => {
    expect(isAnalyticsSource("ga4")).toBe(true);
    expect(isAnalyticsSource("search_console")).toBe(true);
    expect(isAnalyticsSource("csv")).toBe(false);
    expect(isAnalyticsSource(undefined)).toBe(false);
  });
});

describe("fixed copy", () => {
  it("says the comparison is observed, never causal, and never claims lift", () => {
    expect(OBSERVED_CHANGE_NOTE).toContain("not a causal effect");
    expect(OBSERVED_CHANGE_NOTE).toContain("not a measure of statistical significance");
    expect(OBSERVED_CHANGE_NOTE).toMatch(/ranking impact/);
    expect(OBSERVED_CHANGE_NOTE).toMatch(/revenue/);
    for (const copy of [OBSERVED_CHANGE_NOTE, SOURCE_SUM_CAVEAT, FIRST_PARTY_CAVEAT, SYNTHETIC_BANNER_NOTE]) {
      expect(copy).not.toMatch(/\b(?:uplift|lift)\b/i);
    }
  });

  it("names the exact commands that produce data", () => {
    const commands = IMPORT_COMMANDS.map((entry) => entry.command);
    expect(commands.some((command) => command.startsWith("make analytics-import "))).toBe(true);
    expect(commands).toContain("make analytics-aggregate");
    expect(commands).toContain("make analytics-demo-seed");
    // An operator must never be shown a command the CLI would reject. `--source` is the row's
    // ORIGIN; "csv" is not one of its choices (a CSV import is chosen by passing FILE=).
    const accepted = ["ga4", "gsc", "first_party", "search_console"];
    for (const command of commands) {
      const source = /\bSOURCE=(\S+)/.exec(command)?.[1];
      if (source !== undefined) expect(accepted).toContain(source);
      expect(command).not.toMatch(/\bSOURCE=csv\b/);
    }
  });
});

describe("number formatting", () => {
  it("never shows 0 % for a CTR without impressions", () => {
    expect(formatCtr(null)).toBe("—");
    expect(formatCtr(undefined)).toBe("—");
    expect(formatCtr(Number.NaN)).toBe("—");
    expect(formatCtr(0)).toBe("0.00%");
    expect(formatCtr(0.025)).toBe("2.50%");
    // A source may count clicks without impressions, so a CTR above 1 is possible.
    expect(formatCtr(1.5)).toBe("150.00%");
  });

  it("formats means, deltas, ratios and positions, with an em dash for nothing", () => {
    expect(formatMean(null)).toBe("—");
    expect(formatMean(5)).toBe("5.0");
    expect(formatDelta(null)).toBe("—");
    expect(formatDelta(2.04)).toBe("+2.0");
    expect(formatDelta(-2.04)).toBe("−2.0");
    expect(formatDelta(0)).toBe("0.0");
    expect(formatDelta(-0.001)).toBe("0.0");
    expect(formatRatio(null)).toBe("—");
    expect(formatRatio(1.4)).toBe("1.40×");
    expect(formatPosition(null)).toBe("—");
    expect(formatPosition(12.44)).toBe("12.4");
  });
});

describe("totals", () => {
  it("returns a single source verbatim and recomputes a sum", () => {
    const only = metricTotals({ impressions: 0, clicks: 3, ctr: null });
    expect(combineTotals([only])).toBe(only);
    expect(combineTotals([metricTotals({ impressions: 100, clicks: 4, ctr: 0.04 }), metricTotals({ impressions: 300, clicks: 16, ctr: 0.0533 })])).toEqual({
      impressions: 400,
      clicks: 20,
      ctr: 0.05,
    });
    expect(combineTotals([])).toEqual({ impressions: 0, clicks: 0, ctr: null });
    expect(combineTotals([metricTotals({ impressions: 0, clicks: 0, ctr: null }), metricTotals({ impressions: 0, clicks: 0, ctr: null })]).ctr).toBeNull();
  });

  it("totals one source or all of them, and reports an absent source as zero", () => {
    const totals = { ga4: metricTotals({ impressions: 10, clicks: 1, ctr: 0.1 }), first_party: metricTotals({ impressions: 90, clicks: 9, ctr: 0.1 }) };
    expect(totalsFor(totals)).toEqual({ impressions: 100, clicks: 10, ctr: 0.1 });
    expect(totalsFor(totals, "ga4")).toEqual({ impressions: 10, clicks: 1, ctr: 0.1 });
    expect(totalsFor(totals, "search_console")).toEqual({ impressions: 0, clicks: 0, ctr: null });
    expect(sourcesOf(totals)).toEqual(["first_party", "ga4"]);
    expect(hasNoEvents({ ga4: metricTotals({ impressions: 0, clicks: 0, ctr: null }) })).toBe(true);
    expect(hasNoEvents(totals)).toBe(false);
    expect(hasNoEvents({})).toBe(true);
  });
});

describe("daily series", () => {
  it("folds (day, source) rows into one point per day, oldest first", () => {
    const points = foldDailyTotals([
      dailyTotals({ metric_date: "2026-09-02", source: "ga4", impressions: 5, clicks: 1 }),
      dailyTotals({ metric_date: "2026-09-01", source: "ga4", impressions: 7, clicks: 2 }),
      dailyTotals({ metric_date: "2026-09-01", source: "first_party", impressions: 3, clicks: 1 }),
    ]);
    expect(points).toEqual([
      { date: "2026-09-01", impressions: 10, clicks: 3 },
      { date: "2026-09-02", impressions: 5, clicks: 1 },
    ]);
  });

  it("filters to one source", () => {
    const rows = [
      dailyTotals({ metric_date: "2026-09-01", source: "ga4", impressions: 7, clicks: 2 }),
      dailyTotals({ metric_date: "2026-09-01", source: "first_party", impressions: 3, clicks: 1 }),
    ];
    expect(foldDailyTotals(rows, "ga4")).toEqual([{ date: "2026-09-01", impressions: 7, clicks: 2 }]);
    expect(foldDailyTotals(rows, "search_console")).toEqual([]);
    expect(foldDailyTotals([])).toEqual([]);
  });

  it("sorts a link series by day", () => {
    const points = linkSeriesPoints([...linkDailyPoints(3)].reverse());
    expect(points.map((point) => point.date)).toEqual(["2026-09-01", "2026-09-02", "2026-09-03"]);
  });
});

describe("the range form", () => {
  it("accepts both dates or neither", () => {
    expect(parseRange({})).toMatchObject({ kind: "absent", startText: "", endText: "" });
    expect(parseRange({ start: "2026-09-01" })).toMatchObject({
      kind: "invalid",
      message: "Give both a start and an end date, or neither.",
      startText: "2026-09-01",
    });
    expect(parseRange({ end: "2026-09-28" })).toMatchObject({ kind: "invalid" });
    expect(parseRange({ start: "2026-09-01", end: "2026-09-28" })).toMatchObject({
      kind: "ok",
      start: "2026-09-01",
      end: "2026-09-28",
    });
  });

  it("rejects a date that is not a real calendar day", () => {
    expect(isIsoDate("2026-02-28")).toBe(true);
    expect(isIsoDate("2026-02-30")).toBe(false);
    expect(isIsoDate("2026-13-01")).toBe(false);
    expect(isIsoDate("01/09/2026")).toBe(false);
    expect(parseRange({ start: "2026-02-30", end: "2026-03-05" })).toMatchObject({
      kind: "invalid",
      message: "Dates must be real calendar days in YYYY-MM-DD form.",
    });
    expect(parseRange({ start: "not-a-date", end: "also-not" }).kind).toBe("invalid");
  });

  it("rejects a reversed range and a span over 400 days", () => {
    expect(parseRange({ start: "2026-09-28", end: "2026-09-01" })).toMatchObject({
      kind: "invalid",
      message: "The end date must not be before the start date.",
    });
    expect(daySpan("2026-09-01", "2026-09-01")).toBe(1);
    expect(daySpan("2026-09-01", "2026-09-28")).toBe(28);
    const lastGood = shiftDay("2026-01-01", MAX_RANGE_DAYS - 1);
    expect(parseRange({ start: "2026-01-01", end: lastGood }).kind).toBe("ok");
    const tooFar = shiftDay("2026-01-01", MAX_RANGE_DAYS);
    const refused = parseRange({ start: "2026-01-01", end: tooFar });
    expect(refused.kind).toBe("invalid");
    expect(refused.kind === "invalid" && refused.message).toContain("at most 400 days");
  });

  it("sends nothing when the range is absent or invalid, and keeps the text for the form", () => {
    expect(rangeQuery(parseRange({}))).toEqual({});
    expect(rangeQuery(parseRange({ start: "2026-02-30", end: "2026-03-05" }))).toEqual({});
    expect(rangeQuery(parseRange({ start: "2026-09-01", end: "2026-09-28" }))).toEqual({
      start: "2026-09-01",
      end: "2026-09-28",
    });
    expect(parseRange({ start: "x".repeat(200), end: "2026-09-28" }).startText).toHaveLength(40);
  });

  it("describes a range with its inclusive day count", () => {
    expect(describeRange({ start: "2026-09-01", end: "2026-09-28" })).toBe("2026-09-01 to 2026-09-28 (28 days)");
    expect(describeRange({ start: "2026-09-01", end: "2026-09-01" })).toBe("2026-09-01 to 2026-09-01 (1 day)");
  });
});

describe("the comparison window", () => {
  it("keeps window_days inside the contract's 7-90", () => {
    expect(parseWindowDays({})).toBe(28);
    expect(parseWindowDays({ window_days: "7" })).toBe(7);
    expect(parseWindowDays({ window_days: "90" })).toBe(90);
    expect(parseWindowDays({ window_days: "6" })).toBe(28);
    expect(parseWindowDays({ window_days: "91" })).toBe(28);
    expect(parseWindowDays({ window_days: "-7" })).toBe(28);
    expect(parseWindowDays({ window_days: "abc" })).toBe(28);
  });

  it("states coverage exactly, and defers sufficiency to the API's own verdict", () => {
    expect(coverageNote("pre", { covered_days: 9, days: 28 })).toBe("Pre-window has 9 of 28 days");
    expect(coverageNote("post", { covered_days: 28, days: 28 })).toBe("Post-window has 28 of 28 days");
    const thin = pageWindowComparison({
      pre: windowStats({ covered_days: 9, mean_per_day: null }),
      delta_per_day: null,
      ratio: null,
      flags: ["insufficient_data"],
    });
    expect(comparisonCoverage(thin)).toEqual({ comparable: false, notes: ["Pre-window has 9 of 28 days"] });
    expect(comparisonCoverage(pageWindowComparison())).toEqual({ comparable: true, notes: [] });
    // Both windows short: both notes, in window order.
    const both = pageWindowComparison({
      pre: windowStats({ covered_days: 2 }),
      post: windowStats({ covered_days: 3 }),
      delta_per_day: null,
    });
    expect(comparisonCoverage(both).notes).toEqual(["Pre-window has 2 of 28 days", "Post-window has 3 of 28 days"]);
  });

  it("reads the metric the API compared", () => {
    expect(windowTotal(windowStats({ clicks: 140, pageviews: 560 }), "clicks")).toBe(140);
    expect(windowTotal(windowStats({ clicks: 140, pageviews: 560 }), "pageviews")).toBe(560);
  });
});

describe("the live window", () => {
  it("tells apart live, closed and never live", () => {
    expect(describeLiveWindow({ plan_id: "p", deployed_at: "2026-09-01T08:00:00Z", rolled_back_at: null })).toMatchObject({
      label: "Live",
      closed: false,
      neverLive: false,
    });
    expect(
      describeLiveWindow({ plan_id: "p", deployed_at: "2026-09-01T08:00:00Z", rolled_back_at: "2026-09-10T08:00:00Z" }),
    ).toMatchObject({ label: "Window closed", closed: true, neverLive: false });
    expect(describeLiveWindow({ plan_id: "p", deployed_at: null, rolled_back_at: null })).toMatchObject({
      label: "Never on a page",
      neverLive: true,
    });
  });

  it("words the no-events case exactly", () => {
    expect(noEventsNote({ plan_id: "p", deployed_at: "2026-09-01T08:00:00Z", rolled_back_at: null })).toBe(
      "Live since 2026-09-01 — no events recorded",
    );
    expect(noEventsNote({ plan_id: "p", deployed_at: null, rolled_back_at: null })).toBe("Live — no events recorded");
  });

  it("knows which plans were ever on a page", () => {
    for (const status of ["deployed", "verified", "failed", "rolled_back", "measured", "learning_captured"]) {
      expect(wasEverApplied({ status })).toBe(true);
    }
    for (const status of ["proposed", "awaiting_approval", "approved", "rejected", "superseded", "expired"]) {
      expect(wasEverApplied({ status })).toBe(false);
    }
    // A plan that moved on from `deployed` is still recognised through its history.
    expect(wasEverApplied({ status: "expired", transitions: [{ to_status: "deployed" }] })).toBe(true);
    expect(wasEverApplied({ status: "rejected", transitions: [{ to_status: "rejected" }] })).toBe(false);
  });
});

describe("synthetic data", () => {
  it("shows the banner as soon as any part of a view is synthetic", () => {
    expect(hasSynthetic(false, false)).toBe(false);
    expect(hasSynthetic(false, true)).toBe(true);
    expect(hasSynthetic(undefined)).toBe(false);
    expect(hasSynthetic()).toBe(false);
  });
});

describe("routes and navigation", () => {
  it("builds a page-impact href, encoding the id", () => {
    expect(pageImpactHref("11111111-1111-4111-8111-111111111111")).toBe(
      "/analytics/pages/11111111-1111-4111-8111-111111111111",
    );
    expect(pageImpactHref("a/b")).toBe("/analytics/pages/a%2Fb");
  });

  it("lists Analytics for every signed-in role (it needs only `read`)", () => {
    expect(NAV_LINKS.find((link) => link.href === "/analytics")).toEqual({ href: "/analytics", label: "Analytics" });
    for (const permissions of [new Set(["read"]), new Set(["read", "review"]), new Set(["read", "deploy", "read_audit", "read_settings"])]) {
      expect(navLinksFor(permissions).map((link) => link.label)).toContain("Analytics");
    }
    expect(activeNavHref("/analytics")).toBe("/analytics");
    expect(activeNavHref("/analytics/pages/abc")).toBe("/analytics");
  });
});

describe("source sets", () => {
  it("offers only sources that can hold the data in question", async () => {
    const { LINK_SOURCES, PAGE_SOURCES } = await import("@/lib/analytics");
    // Search Console has no link-level data (ADR-0015 section 12.9.2).
    expect(LINK_SOURCES).toEqual<AnalyticsSource[]>(["ga4", "first_party"]);
    expect(PAGE_SOURCES).toEqual<AnalyticsSource[]>(["ga4", "search_console"]);
    expect(CONFOUNDER_FLAGS).toContain<ConfounderFlag>("synthetic_data");
  });
});
