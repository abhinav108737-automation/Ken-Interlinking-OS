import type { GraphClusterSummary, GraphOverview, GraphPageRow, GraphSignals } from "@/lib/api-client";

import { graphSignals } from "./recommendations";
import { OTHER_PAGE_ID, PAGE_ID } from "./inventory";

export function graphOverview(overrides: Partial<GraphOverview> = {}): GraphOverview {
  return {
    pages: 65,
    // 4 flagged is_orphan = 3 true orphans + the crawl entry page.
    orphans: 4,
    true_orphans: 3,
    entry_pages: 1,
    nearly_orphans: 9,
    bridges: 5,
    components: 2,
    clusters: 7,
    metrics_version: "graph-alg1",
    computed_at: "2026-10-15T08:00:00Z",
    ...overrides,
  };
}

export function graphCluster(overrides: Partial<GraphClusterSummary> = {}): GraphClusterSummary {
  return {
    cluster_id: 3,
    pages: 12,
    orphans: 2,
    true_orphans: 1,
    nearly_orphans: 3,
    bridges: 1,
    mean_in_degree: 2.5,
    top_page_id: OTHER_PAGE_ID,
    top_page_url: "https://www.example.com/reports/lithium-ion-battery-market",
    ...overrides,
  };
}

export function graphPageRow(
  overrides: Partial<GraphPageRow> = {},
  signals: Partial<GraphSignals> = {},
): GraphPageRow {
  return {
    canonical_url: "https://www.example.com/reports/widgets",
    title: "Widgets market report",
    is_entry_page: false,
    signals: graphSignals({ page_id: PAGE_ID, is_orphan: false, is_nearly_orphan: false, in_degree: 4, ...signals }),
    ...overrides,
  };
}
