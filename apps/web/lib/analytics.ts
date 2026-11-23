/**
 * Phase 7 analytics: presentation and form logic (ADR-0015 sections 6-7, and
 * section 12, which is binding).
 *
 * Portable and pure (no React, no fetch, no Node), like lib/deployments.ts and
 * lib/status.ts: every enum mapping is a `Record` keyed by a generated contract
 * enum, so a new contract value fails type-checking until it is labelled, and an
 * unexpected runtime value degrades to an explicit "Unknown (…)" label.
 *
 * Two rules govern every string in this module:
 *
 * 1. **Never imply causality.** A before/after window is an OBSERVED CHANGE.
 *    Nothing here (or in the screens) may claim lift, significance, ranking
 *    impact or revenue (ADR-0015 section 3, "What we do NOT claim").
 * 2. **Synthetic data must be unmistakable.** Demo rows carry `synthetic`, and
 *    every screen that shows one renders a banner and a per-row badge.
 *
 * Landing events, query metrics and experiments are OUT of Phase 7
 * (ADR-0015 sections 12.2 and 12.8), so nothing here mentions them.
 */
import type {
  AnalyticsSource,
  ComparisonMetric,
  ComparisonRole,
  ConfounderFlag,
  Dataset,
  DailyTotals,
  ImportVia,
  LinkDailyPoint,
  LinkPlanStatus,
  LinkSort,
  LiveWindow,
  MetricTotals,
  PageWindowComparison,
  WindowStats,
} from "./api-client";
import { NONE, formatCount } from "./format";
import { type SearchParams, firstParam } from "./inventory";
import type { StatusPresentation } from "./status";

/** The policy's minimum covered days per window before a change is shown (ADR-0015 section 3). */
export const MIN_COVERED_DAYS = 14;

function hasOwn<T extends object>(record: T, key: string): key is Extract<keyof T, string> {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function unknownLabel(value: string): string {
  return `Unknown (${value.trim() === "" ? "empty" : value})`;
}

function label<K extends string>(table: Readonly<Record<K, string>>, value: string): string {
  return hasOwn(table, value) ? table[value] : unknownLabel(value);
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

export const ANALYTICS_PATH = "/analytics";

/** The page-impact screen of one page. */
export function pageImpactHref(pageId: string): string {
  return `${ANALYTICS_PATH}/pages/${encodeURIComponent(pageId)}`;
}

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

interface Labelled {
  readonly label: string;
  readonly description: string;
}

const SOURCE: Readonly<Record<AnalyticsSource, Labelled>> = {
  ga4: {
    label: "GA4",
    description:
      "Google Analytics 4. Link-level rows come from the forwarded link events; page-level rows are pageviews and sessions.",
  },
  search_console: {
    label: "Search Console",
    description:
      "Google Search Console. Page and query metrics include impressions, clicks and average position; it has no link-level data.",
  },
  first_party: {
    label: "First-party",
    description: "Ken's own pseudonymous link events: impressions and clicks counted per day, with no visitor identifier.",
  },
};

/** Sources that can carry LINK-level rows at all (ADR-0015 section 12.9.2). */
export const LINK_SOURCES: readonly AnalyticsSource[] = ["ga4", "first_party"];
/** Sources that can carry PAGE-level rows. */
export const PAGE_SOURCES: readonly AnalyticsSource[] = ["ga4", "search_console"];

const DATASET: Readonly<Record<Dataset, string>> = {
  links: "Links",
  pages: "Pages",
  queries: "Queries",
};

const IMPORT_VIA: Readonly<Record<ImportVia, string>> = {
  api: "Provider API",
  csv: "CSV file",
  collector: "First-party collector",
  synthetic: "Synthetic (demo seed)",
};

const LINK_SORT: Readonly<Record<LinkSort, string>> = {
  clicks: "Clicks",
  impressions: "Impressions",
  ctr: "Click-through rate",
};

const METRIC: Readonly<Record<ComparisonMetric, string>> = {
  clicks: "Clicks",
  pageviews: "Pageviews",
};

const ROLE: Readonly<Record<ComparisonRole, string>> = {
  source: "Source page (the link was added here)",
  target: "Target page (the link points here)",
};

/**
 * Why an observed change may have nothing to do with the link. Every flag is a
 * reason for caution, never a correction: no flag is ever applied to a number.
 */
const FLAG: Readonly<Record<ConfounderFlag, StatusPresentation>> = {
  overlapping_changes: {
    label: "Other links changed",
    description:
      "Another link was applied to this page inside one of the windows, so the two changes cannot be told apart.",
    tone: "caution",
    known: true,
  },
  content_changed: {
    label: "Content changed",
    description: "A new version of this page was stored inside one of the windows, so the page itself is not constant.",
    tone: "caution",
    known: true,
  },
  post_window_truncated: {
    label: "After-window incomplete",
    description: "The after-window was cut short by a rollback or by the last day of imported data.",
    tone: "caution",
    known: true,
  },
  rolled_back_in_window: {
    label: "Rolled back in window",
    description: "The link was removed from the page during one of the windows.",
    tone: "caution",
    known: true,
  },
  insufficient_data: {
    label: "Not enough data",
    description: `A window has fewer than ${MIN_COVERED_DAYS} days with stored data, so no change is shown.`,
    tone: "neutral",
    known: true,
  },
  sampled_data: {
    label: "Sampled data",
    description: "The provider reported this data as sampled, so the counts are estimates.",
    tone: "caution",
    known: true,
  },
  synthetic_data: {
    label: "Synthetic data",
    description: "These numbers come from the local demo seed. They are made up and describe nothing real.",
    tone: "critical",
    known: true,
  },
};

export const ANALYTICS_SOURCES = Object.keys(SOURCE) as readonly AnalyticsSource[];
export const DATASETS = Object.keys(DATASET) as readonly Dataset[];
export const IMPORT_VIAS = Object.keys(IMPORT_VIA) as readonly ImportVia[];
export const LINK_SORTS = Object.keys(LINK_SORT) as readonly LinkSort[];
export const CONFOUNDER_FLAGS = Object.keys(FLAG) as readonly ConfounderFlag[];

export const describeSource = (value: string): string => (hasOwn(SOURCE, value) ? SOURCE[value].label : unknownLabel(value));
export const describeSourceDetail = (value: string): string | null =>
  hasOwn(SOURCE, value) ? SOURCE[value].description : null;
export const describeDataset = (value: string): string => label(DATASET, value);
export const describeImportVia = (value: string): string => label(IMPORT_VIA, value);
export const describeLinkSort = (value: string): string => label(LINK_SORT, value);
export const describeComparisonMetric = (value: string): string => label(METRIC, value);
export const describeComparisonRole = (value: string): string => label(ROLE, value);

export function describeConfounderFlag(value: string): StatusPresentation {
  return hasOwn(FLAG, value)
    ? FLAG[value]
    : {
        label: unknownLabel(value),
        description: "The API reported a confounder this console does not recognise.",
        tone: "neutral",
        known: false,
      };
}

export function isAnalyticsSource(value: unknown): value is AnalyticsSource {
  return typeof value === "string" && hasOwn(SOURCE, value);
}

// ---------------------------------------------------------------------------
// Fixed copy. The causality wording is binding (ADR-0015 sections 3 and 7).
// ---------------------------------------------------------------------------

export const OBSERVED_CHANGE_TITLE = "Observed change, not a causal effect";

export const OBSERVED_CHANGE_NOTE =
  "These numbers are the observed change in a page's own metrics before and after a link was added. They are not a " +
  "causal effect, and they are not a measure of statistical significance, ranking impact or revenue. Seasonality, " +
  "search-engine updates, sitewide trends, other content changes and other links applied at the same time all move " +
  "these numbers. Read them as context for a human decision, never as proof that the link caused the change.";

/** Shown wherever first-party counts are presented as evidence. */
export const FIRST_PARTY_CAVEAT =
  "First-party counts are collected from browsers, so they can be inflated and must never be the only basis for a decision.";

/** The sum over sources is not a count of unique events: two sources can see the same link. */
export const SOURCE_SUM_CAVEAT =
  "Each source counts independently, so a total over several sources is not a count of unique impressions or clicks.";

export const SYNTHETIC_BANNER_TITLE = "Synthetic demo data";

export const SYNTHETIC_BANNER_NOTE =
  "This screen includes synthetic data generated by the local demo seed (make analytics-demo-seed). It is made up: it " +
  "describes no real visitor, page or link, and it never moves a recommendation to “measured”. Every synthetic series " +
  "and row is also badged below.";

export const SYNTHETIC_BADGE_LABEL = "Synthetic";

/** The exact commands that produce analytics data. Shown verbatim when there is nothing to show. */
export const IMPORT_COMMANDS: readonly { readonly command: string; readonly what: string }[] = [
  {
    command:
      "make analytics-import SOURCE=first_party DATASET=links START=2026-09-01 END=2026-09-28 FILE=path/to/ken_link_daily.csv",
    what:
      "Import daily rows out of a canonical CSV. SOURCE is the origin of the rows " +
      "(ga4 | gsc | first_party) and must match the file's source column; DATASET is links | pages; " +
      "giving FILE is what makes it a CSV import. Without FILE, ga4 and gsc call Google, which is off " +
      "by default.",
  },
  { command: "make analytics-aggregate", what: "Aggregate first-party link events into daily rows, then run the measure sweep." },
  { command: "make analytics-demo-seed", what: "Seed clearly-marked synthetic data for a local demo. Never real data." },
];

// ---------------------------------------------------------------------------
// Numbers
// ---------------------------------------------------------------------------

/**
 * A click-through rate as a percentage. `null` (no impressions) renders as an
 * em dash, NEVER as 0 %: "no impressions" and "nobody clicked" are different.
 */
export function formatCtr(ctr: number | null | undefined): string {
  if (typeof ctr !== "number" || !Number.isFinite(ctr)) return NONE;
  return `${(ctr * 100).toFixed(2)}%`;
}

/** A per-day mean, to one decimal. `null` (an empty or insufficient window) → em dash. */
export function formatMean(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(1) : NONE;
}

/** A signed difference of per-day means, to one decimal. `null` → em dash. */
export function formatDelta(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NONE;
  const text = Math.abs(value).toFixed(1);
  if (Number.parseFloat(text) === 0) return "0.0";
  return `${value > 0 ? "+" : "−"}${text}`;
}

/** A ratio of per-day means, as a multiplier. `null` → em dash. */
export function formatRatio(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(2)}×` : NONE;
}

/** An average search position, to one decimal. `null` → em dash. */
export function formatPosition(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(1) : NONE;
}

/** Sum `MetricTotals`; a single entry is returned verbatim so the API's own `ctr` survives. */
export function combineTotals(totals: readonly MetricTotals[]): MetricTotals {
  if (totals.length === 1) return totals[0]!;
  let impressions = 0;
  let clicks = 0;
  for (const entry of totals) {
    impressions += entry.impressions;
    clicks += entry.clicks;
  }
  return { impressions, clicks, ctr: impressions === 0 ? null : clicks / impressions };
}

/** Totals of a `{source: MetricTotals}` map, optionally for one source only. */
export function totalsFor(
  totals: Readonly<Record<string, MetricTotals>>,
  source?: AnalyticsSource,
): MetricTotals {
  const entries =
    source === undefined
      ? Object.keys(totals)
          .sort()
          .map((key) => totals[key]!)
      : totals[source] === undefined
        ? []
        : [totals[source]!];
  return entries.length === 0 ? { impressions: 0, clicks: 0, ctr: null } : combineTotals(entries);
}

/** The sources of a `{source: …}` map, in a stable order. */
export function sourcesOf(record: Readonly<Record<string, unknown>>): string[] {
  return Object.keys(record).sort((a, b) => a.localeCompare(b, "en"));
}

// ---------------------------------------------------------------------------
// Daily series
// ---------------------------------------------------------------------------

export interface SeriesPoint {
  /** The day, `YYYY-MM-DD`. */
  readonly date: string;
  readonly impressions: number;
  readonly clicks: number;
}

/**
 * `DailyTotals` rows (one per day and source) folded into one point per day,
 * optionally for a single source. Days are summed, not averaged; see
 * `SOURCE_SUM_CAVEAT` for why a multi-source sum is not a unique count.
 */
export function foldDailyTotals(daily: readonly DailyTotals[], source?: AnalyticsSource): SeriesPoint[] {
  const byDate = new Map<string, { impressions: number; clicks: number }>();
  for (const row of daily) {
    if (source !== undefined && row.source !== source) continue;
    const point = byDate.get(row.metric_date) ?? { impressions: 0, clicks: 0 };
    point.impressions += row.impressions;
    point.clicks += row.clicks;
    byDate.set(row.metric_date, point);
  }
  return [...byDate.entries()]
    .map(([date, point]) => ({ date, ...point }))
    .sort((a, b) => a.date.localeCompare(b.date, "en"));
}

/** One link source's daily points as series points, oldest first. */
export function linkSeriesPoints(points: readonly LinkDailyPoint[]): SeriesPoint[] {
  return [...points]
    .map((point) => ({ date: point.metric_date, impressions: point.impressions, clicks: point.clicks }))
    .sort((a, b) => a.date.localeCompare(b.date, "en"));
}

// ---------------------------------------------------------------------------
// The date-range form (a GET form: it only changes the query string)
// ---------------------------------------------------------------------------

/** The contract's maximum span of `start`..`end`, inclusive (ADR-0015 section 6). */
export const MAX_RANGE_DAYS = 400;

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** True for a real calendar day in `YYYY-MM-DD` (so 2026-02-30 is rejected). */
export function isIsoDate(value: string): boolean {
  if (!ISO_DATE.test(value)) return false;
  const time = Date.parse(`${value}T00:00:00Z`);
  if (Number.isNaN(time)) return false;
  return new Date(time).toISOString().slice(0, 10) === value;
}

const MS_PER_DAY = 86_400_000;

/** Inclusive number of days from `start` to `end`; negative when `end` is before `start`. */
export function daySpan(start: string, end: string): number {
  return (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / MS_PER_DAY + 1;
}

/** A day shifted by `days`, as `YYYY-MM-DD`. */
export function shiftDay(day: string, days: number): string {
  return new Date(Date.parse(`${day}T00:00:00Z`) + days * MS_PER_DAY).toISOString().slice(0, 10);
}

export type RangeSelection =
  | { readonly kind: "absent"; readonly startText: string; readonly endText: string }
  | { readonly kind: "ok"; readonly start: string; readonly end: string; readonly startText: string; readonly endText: string }
  | { readonly kind: "invalid"; readonly message: string; readonly startText: string; readonly endText: string };

/**
 * `start` and `end` from the query string, validated exactly as the contract
 * does: both or neither, real ISO days, end not before start, and a span of at
 * most 400 days. An invalid range is NOT sent: the screen says why and the API
 * then answers for its own default range.
 */
export function parseRange(params: SearchParams): RangeSelection {
  const startText = (firstParam(params, "start") ?? "").slice(0, 40);
  const endText = (firstParam(params, "end") ?? "").slice(0, 40);
  const texts = { startText, endText };
  if (startText === "" && endText === "") return { kind: "absent", ...texts };
  if (startText === "" || endText === "") {
    return { kind: "invalid", message: "Give both a start and an end date, or neither.", ...texts };
  }
  if (!isIsoDate(startText) || !isIsoDate(endText)) {
    return { kind: "invalid", message: "Dates must be real calendar days in YYYY-MM-DD form.", ...texts };
  }
  const span = daySpan(startText, endText);
  if (span < 1) return { kind: "invalid", message: "The end date must not be before the start date.", ...texts };
  if (span > MAX_RANGE_DAYS) {
    return { kind: "invalid", message: `The range may cover at most ${MAX_RANGE_DAYS} days; this one covers ${span}.`, ...texts };
  }
  return { kind: "ok", start: startText, end: endText, ...texts };
}

/** `{start, end}` to send, or `{}` when the range is absent or invalid (the API then defaults). */
export function rangeQuery(range: RangeSelection): { start?: string; end?: string } {
  return range.kind === "ok" ? { start: range.start, end: range.end } : {};
}

/** A range as text, e.g. "2026-09-01 to 2026-09-28 (28 days)". */
export function describeRange(range: { readonly start: string; readonly end: string }): string {
  const span = daySpan(range.start, range.end);
  const days = Number.isFinite(span) ? ` (${formatCount(span)} ${span === 1 ? "day" : "days"})` : "";
  return `${range.start} to ${range.end}${days}`;
}

// ---------------------------------------------------------------------------
// The page-impact window
// ---------------------------------------------------------------------------

export const MIN_WINDOW_DAYS = 7;
export const MAX_WINDOW_DAYS = 90;
/** The policy's window length (ADR-0015 section 3). The API owns the real value. */
export const DEFAULT_WINDOW_DAYS = 28;

export const WINDOW_DAY_CHOICES: readonly number[] = [7, 14, 28, 60, 90];

/** `window_days` within the contract's 7–90; anything else falls back to the default. */
export function parseWindowDays(params: SearchParams, name = "window_days"): number {
  const raw = firstParam(params, name);
  if (raw === undefined || !/^\d{1,3}$/.test(raw)) return DEFAULT_WINDOW_DAYS;
  const value = Number.parseInt(raw, 10);
  return value >= MIN_WINDOW_DAYS && value <= MAX_WINDOW_DAYS ? value : DEFAULT_WINDOW_DAYS;
}

// ---------------------------------------------------------------------------
// Window coverage and sufficiency
// ---------------------------------------------------------------------------

/** "Pre-window has 9 of 28 days" — the exact shape of the insufficiency note. */
export function coverageNote(which: "pre" | "post", stats: Pick<WindowStats, "covered_days" | "days">): string {
  const name = which === "pre" ? "Pre-window" : "Post-window";
  return `${name} has ${formatCount(stats.covered_days)} of ${formatCount(stats.days)} days`;
}

export interface ComparisonCoverage {
  /** True when the API computed a change (both windows were sufficient). */
  readonly comparable: boolean;
  /** The coverage notes of the windows that are short of `MIN_COVERED_DAYS`. */
  readonly notes: readonly string[];
}

/**
 * Whether a comparison may be read as a change at all. `delta_per_day` is the
 * API's own verdict: it is null unless BOTH windows had enough covered days, so
 * the console never recomputes sufficiency, it only explains it.
 */
export function comparisonCoverage(comparison: PageWindowComparison): ComparisonCoverage {
  const notes: string[] = [];
  if (comparison.pre.covered_days < MIN_COVERED_DAYS) notes.push(coverageNote("pre", comparison.pre));
  if (comparison.post.covered_days < MIN_COVERED_DAYS) notes.push(coverageNote("post", comparison.post));
  return { comparable: comparison.delta_per_day !== null, notes };
}

/** The metric's total in a window (the metric the API compared). */
export function windowTotal(stats: WindowStats, metric: ComparisonMetric): number {
  return metric === "clicks" ? stats.clicks : stats.pageviews;
}

// ---------------------------------------------------------------------------
// Live window
// ---------------------------------------------------------------------------

export interface LiveWindowPresentation {
  readonly label: string;
  readonly description: string;
  readonly tone: StatusPresentation["tone"];
  /** True once the link has been removed from the page again. */
  readonly closed: boolean;
  /** True when the link was never on a page (superseded, rejected, never applied). */
  readonly neverLive: boolean;
}

/**
 * The window in which events can be attributed to a link: `[deployed_at,
 * rolled_back_at)` from the plan's transitions (ADR-0015 section 12.6.3).
 */
export function describeLiveWindow(window: LiveWindow): LiveWindowPresentation {
  if (window.deployed_at === null) {
    return {
      label: "Never on a page",
      description:
        "This recommendation was never deployed (it was superseded, rejected or never applied), so no event can be attributed to it.",
      tone: "neutral",
      closed: false,
      neverLive: true,
    };
  }
  if (window.rolled_back_at !== null) {
    return {
      label: "Window closed",
      description: "The link was rolled back, so its live window has closed. No data is collected for it after that moment.",
      tone: "neutral",
      closed: true,
      neverLive: false,
    };
  }
  return {
    label: "Live",
    description: "The link is on the page now, so events are still being attributed to it.",
    tone: "positive",
    closed: false,
    neverLive: false,
  };
}

/** True when a link has a live window but not a single stored event in range. */
export function hasNoEvents(totals: Readonly<Record<string, MetricTotals>>): boolean {
  const combined = totalsFor(totals);
  return combined.impressions === 0 && combined.clicks === 0;
}

/** "Live since 2026-09-01 — no events recorded" for a live link with nothing stored. */
export function noEventsNote(window: LiveWindow): string {
  const since = window.deployed_at === null ? "" : ` since ${window.deployed_at.slice(0, 10)}`;
  return `Live${since} — no events recorded`;
}

/**
 * Plan statuses that mean the link was (or may still be) on a page, so a
 * performance panel is worth showing. `failed` is included deliberately: a
 * failed plan's link may be on the page, which is why the live-link index
 * accepts its events (ADR-0015 section 12.1e).
 */
const EVER_ON_PAGE: ReadonlySet<string> = new Set<LinkPlanStatus>([
  "deployed",
  "verified",
  "failed",
  "rolled_back",
  "measured",
  "learning_captured",
]);

/**
 * Whether a plan was ever applied to a page, from its own status history: a
 * transition to `deployed` is the live window's start (ADR-0015 section 12.6.3).
 */
export function wasEverApplied(plan: {
  readonly status: string;
  readonly transitions?: readonly { readonly to_status: string }[];
}): boolean {
  return (
    EVER_ON_PAGE.has(plan.status) || (plan.transitions ?? []).some((transition) => transition.to_status === "deployed")
  );
}

// ---------------------------------------------------------------------------
// Synthetic data
// ---------------------------------------------------------------------------

/** True when any part of a view carries synthetic rows, so the banner must show. */
export function hasSynthetic(...flags: readonly (boolean | undefined)[]): boolean {
  return flags.some((flag) => flag === true);
}
