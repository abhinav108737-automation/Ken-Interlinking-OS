/**
 * Presentation logic for the link-graph diagnostics screens.
 *
 * Pure and portable, with the conventions of lib/status.ts. The one rule that
 * matters here: the API's raw `orphans` count INCLUDES the crawl entry page,
 * because a database CHECK makes `is_orphan = (in_degree = 0)` and the home
 * page has no inbound link. The entry page is not a problem page, so every
 * screen headlines `true_orphans` and labels the entry page as an entry page.
 */
import type { GraphPageRow } from "./api-client";
import { type SearchParams, buildHref, firstParam } from "./inventory";
import type { StatusPresentation } from "./status";

export const GRAPH_PATH = "/graph";
export const GRAPH_PAGES_PATH = "/graph/pages";

/** Plain-language definitions, shown on the screens verbatim. */
export const GRAPH_DEFINITIONS: readonly (readonly [string, string])[] = [
  ["Orphan page", "A page with no internal link pointing at it from any other current page. Readers and crawlers can only reach it by a direct URL."],
  ["Nearly-orphan page", "A page whose inbound internal links are at or below the policy threshold. It is reachable, but barely."],
  ["Bridge page", "A cut vertex: removing this page would split part of the link graph into pieces that can no longer reach each other."],
  ["Entry page", "The page the crawl starts from (crawl depth 0). It usually has no inbound link, so it is flagged as an orphan by the database rule, but it is not a problem page."],
];

export type GraphPosition = "entry_page" | "true_orphan" | "nearly_orphan" | "linked";

const POSITION: Readonly<Record<GraphPosition, StatusPresentation>> = {
  entry_page: {
    label: "Entry page",
    description: "The crawl entry page (depth 0). Flagged orphan by the database rule, but it is not a page to recover links to.",
    tone: "neutral",
    known: true,
  },
  true_orphan: {
    label: "Orphan",
    description: "No internal inbound link from any current page, and not the crawl entry page.",
    tone: "critical",
    known: true,
  },
  nearly_orphan: {
    label: "Nearly orphan",
    description: "Inbound internal links are at or below the policy threshold.",
    tone: "caution",
    known: true,
  },
  linked: {
    label: "Linked",
    description: "Has inbound internal links above the policy threshold.",
    tone: "positive",
    known: true,
  },
};

export const BRIDGE_PRESENTATION: StatusPresentation = {
  label: "Bridge page",
  description: "A cut vertex: removing this page would disconnect part of the link graph.",
  tone: "caution",
  known: true,
};

export function graphPositionOf(row: Pick<GraphPageRow, "is_entry_page" | "signals">): GraphPosition {
  if (row.signals.is_orphan) return row.is_entry_page ? "entry_page" : "true_orphan";
  return row.signals.is_nearly_orphan ? "nearly_orphan" : "linked";
}

export function describeGraphPosition(position: GraphPosition): StatusPresentation {
  return POSITION[position];
}

/** Non-negative integer id (cluster / component ordinals start at 0). Anything else is ignored. */
export function parseOrdinal(params: SearchParams, name: string): number | undefined {
  const raw = firstParam(params, name);
  if (raw === undefined || !/^\d{1,7}$/.test(raw)) return undefined;
  const value = Number.parseInt(raw, 10);
  return value <= 1_000_000 ? value : undefined;
}

/** A checkbox-style flag: `1` or `true`. */
export function parseFlag(params: SearchParams, name: string): boolean {
  const raw = firstParam(params, name);
  return raw === "1" || raw === "true";
}

export function clusterLabel(clusterId: number | null): string {
  return clusterId === null ? "No topic" : `Cluster #${clusterId}`;
}

export function clusterPagesHref(clusterId: number | null): string | null {
  return clusterId === null ? null : buildHref(GRAPH_PAGES_PATH, { cluster_id: clusterId });
}
