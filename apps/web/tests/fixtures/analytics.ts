import type {
  AnalyticsImportView,
  AnalyticsSummary,
  DailyTotals,
  LinkAttribution,
  LinkDailyPoint,
  LinkMeasurement,
  LinkPerformanceDetail,
  LinkPerformanceView,
  LiveWindow,
  MetricTotals,
  PageDailyPoint,
  PageImpactView,
  PageWindowComparison,
  SourceFreshness,
  WindowStats,
} from "@/lib/api-client";

import { OTHER_PAGE_ID, PAGE_ID } from "./inventory";
import { ANCHOR, OLD_PLAN_ID, PLAN_ID, REC_RUN_ID } from "./recommendations";

export const IMPORT_ID = "77777777-7777-4777-8777-777777777777";
export const IMPORT_JOB_RUN_ID = "88888888-8888-4888-8888-888888888888";
export const DEPLOYMENT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
export const SOURCE_URL = "https://www.example.com/reports/widgets";
export const TARGET_URL = "https://www.example.com/reports/lithium-ion-battery-market";

export function metricTotals(overrides: Partial<MetricTotals> = {}): MetricTotals {
  return { impressions: 1000, clicks: 25, ctr: 0.025, ...overrides };
}

export function dailyTotals(overrides: Partial<DailyTotals> = {}): DailyTotals {
  return { metric_date: "2026-09-01", source: "first_party", impressions: 120, clicks: 4, ...overrides };
}

/** `days` consecutive daily totals from `start`, with deterministic counts. */
export function dailySeries(days: number, overrides: Partial<DailyTotals> = {}): DailyTotals[] {
  const start = Date.parse("2026-09-01T00:00:00Z");
  return Array.from({ length: days }, (_unused, index) =>
    dailyTotals({
      metric_date: new Date(start + index * 86_400_000).toISOString().slice(0, 10),
      impressions: 100 + index * 10,
      clicks: 3 + (index % 4),
      ...overrides,
    }),
  );
}

export function sourceFreshness(overrides: Partial<SourceFreshness> = {}): SourceFreshness {
  return {
    source: "first_party",
    dataset: "links",
    latest_date: "2026-09-28",
    last_import_id: IMPORT_ID,
    last_imported_at: "2026-09-29T02:00:00Z",
    last_via: "collector",
    synthetic: false,
    ...overrides,
  };
}

export function linkPerformanceView(overrides: Partial<LinkPerformanceView> = {}): LinkPerformanceView {
  return {
    plan_id: PLAN_ID,
    anchor_text: ANCHOR,
    source_page_id: PAGE_ID,
    source_url: SOURCE_URL,
    target_page_id: OTHER_PAGE_ID,
    target_url: TARGET_URL,
    plan_status: "verified",
    deployed_at: "2026-09-01T08:00:00Z",
    rolled_back_at: null,
    totals: { first_party: metricTotals() },
    synthetic: false,
    ...overrides,
  };
}

export function analyticsSummary(overrides: Partial<AnalyticsSummary> = {}): AnalyticsSummary {
  return {
    range: { start: "2026-09-01", end: "2026-09-28" },
    sources: [sourceFreshness(), sourceFreshness({ source: "ga4", dataset: "pages", last_via: "api", latest_date: "2026-09-27" })],
    totals_by_source: { first_party: metricTotals() },
    daily: dailySeries(3),
    live_links: 7,
    links_with_data: 4,
    top_links: [linkPerformanceView()],
    synthetic: false,
    ...overrides,
  };
}

export function windowStats(overrides: Partial<WindowStats> = {}): WindowStats {
  return {
    start: "2026-08-04",
    end: "2026-08-31",
    days: 28,
    covered_days: 28,
    impressions: 2800,
    clicks: 140,
    pageviews: 560,
    sessions: 420,
    ctr: 0.05,
    avg_position: 12.4,
    mean_per_day: 5,
    ...overrides,
  };
}

export function pageWindowComparison(overrides: Partial<PageWindowComparison> = {}): PageWindowComparison {
  return {
    page_id: OTHER_PAGE_ID,
    plan_id: PLAN_ID,
    source: "search_console",
    role: "target",
    metric: "clicks",
    anchor_date: "2026-09-01",
    pre: windowStats(),
    post: windowStats({ start: "2026-09-02", end: "2026-09-29", clicks: 196, mean_per_day: 7 }),
    delta_per_day: 2,
    ratio: 1.4,
    site_trend_ratio: 1.05,
    flags: ["content_changed"],
    synthetic: false,
    ...overrides,
  };
}

export function liveWindow(overrides: Partial<LiveWindow> = {}): LiveWindow {
  return { plan_id: PLAN_ID, deployed_at: "2026-09-01T08:00:00Z", rolled_back_at: null, ...overrides };
}

export function linkAttribution(overrides: Partial<LinkAttribution> = {}): LinkAttribution {
  return {
    plan_id: PLAN_ID,
    plan_status: "measured",
    anchor_text: ANCHOR,
    anchor_source: "text",
    reason_codes: ["underlinked_target"],
    relationship_type: "related_market",
    supersedes: OLD_PLAN_ID,
    engine_version: "rec-1.2.0",
    policy_version: 3,
    ranking_policy_version: 2,
    analysis_version: "analysis-1.1.0",
    calibration_version: null,
    recommendation_run_id: REC_RUN_ID,
    source_page_id: PAGE_ID,
    source_url: SOURCE_URL,
    source_page_version: 4,
    target_page_id: OTHER_PAGE_ID,
    target_url: TARGET_URL,
    target_page_version: 2,
    agent_run_id: "99999999-9999-4999-8999-999999999999",
    agent_verdict: "accept",
    agent_model: "qwen2.5:14b",
    agent_graph_version: "graph-1.0.0",
    approved_by: "user:olivia",
    approved_at: "2026-08-31T10:00:00Z",
    deployment_id: DEPLOYMENT_ID,
    cms_provider: "simulated",
    deployed_at: "2026-09-01T08:00:00Z",
    verified_at: "2026-09-01T08:00:03Z",
    rolled_back_at: null,
    ...overrides,
  };
}

export function linkDailyPoints(days: number): LinkDailyPoint[] {
  const start = Date.parse("2026-09-01T00:00:00Z");
  return Array.from({ length: days }, (_unused, index) => ({
    metric_date: new Date(start + index * 86_400_000).toISOString().slice(0, 10),
    impressions: 100 + index * 10,
    clicks: 3 + (index % 4),
  }));
}

export function linkMeasurement(overrides: Partial<LinkMeasurement> = {}): LinkMeasurement {
  return {
    plan_id: PLAN_ID,
    measured_at: "2026-09-30T02:00:00Z",
    analytics_policy_version: 1,
    window_start: "2026-09-01",
    window_end: "2026-09-29",
    link_totals: { first_party: metricTotals() },
    target_comparison: [pageWindowComparison()],
    source_comparison: [],
    flags: ["content_changed"],
    import_ids: [IMPORT_ID],
    ...overrides,
  };
}

export function linkPerformanceDetail(overrides: Partial<LinkPerformanceDetail> = {}): LinkPerformanceDetail {
  return {
    attribution: linkAttribution(),
    live_window: liveWindow(),
    range: { start: "2026-09-01", end: "2026-09-28" },
    measurement: null,
    series: { first_party: linkDailyPoints(3) },
    totals: { first_party: metricTotals() },
    target_impact: [],
    source_impact: [],
    synthetic: false,
    ...overrides,
  };
}

export function pageDailyPoint(overrides: Partial<PageDailyPoint> = {}): PageDailyPoint {
  return {
    metric_date: "2026-09-01",
    impressions: 400,
    clicks: 20,
    pageviews: 60,
    sessions: 48,
    avg_position: 11.5,
    ...overrides,
  };
}

export function pageDailyPoints(days: number, overrides: Partial<PageDailyPoint> = {}): PageDailyPoint[] {
  const start = Date.parse("2026-09-01T00:00:00Z");
  return Array.from({ length: days }, (_unused, index) =>
    pageDailyPoint({
      metric_date: new Date(start + index * 86_400_000).toISOString().slice(0, 10),
      clicks: 20 + index,
      pageviews: 60 + index * 2,
      ...overrides,
    }),
  );
}

export function pageImpactView(overrides: Partial<PageImpactView> = {}): PageImpactView {
  return {
    page_id: OTHER_PAGE_ID,
    url: TARGET_URL,
    window_days: 28,
    series: { search_console: pageDailyPoints(3) },
    changes: [pageWindowComparison()],
    synthetic: false,
    ...overrides,
  };
}

export function analyticsImportView(overrides: Partial<AnalyticsImportView> = {}): AnalyticsImportView {
  return {
    id: IMPORT_ID,
    job_run_id: IMPORT_JOB_RUN_ID,
    source: "first_party",
    dataset: "links",
    via: "collector",
    synthetic: false,
    date_start: "2026-09-01",
    date_end: "2026-09-28",
    source_timezone: "UTC",
    file_name: null,
    file_sha256: null,
    sampled: false,
    replace_via: false,
    rows_written: 812,
    analytics_policy_version: 1,
    counters: { rows_read: 900, rows_written: 812, unresolved_link: 4 },
    unresolved_sample: ["/reports/missing"],
    triggered_by: "user:olivia",
    created_at: "2026-09-29T02:00:00Z",
    ...overrides,
  };
}
