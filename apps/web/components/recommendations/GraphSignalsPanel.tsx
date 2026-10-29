import type { GraphSignals } from "@/lib/api-client";
import { formatCount, formatDateTime } from "@/lib/format";
import { formatScore } from "@/lib/intelligence";

import { StatusBadge } from "../StatusBadge";
import { CELL, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const ORPHAN: Parameters<typeof StatusBadge>[0]["presentation"] = {
  label: "Orphan",
  description: "No internal inbound link from any current page.",
  tone: "critical",
  known: true,
};

const NEARLY_ORPHAN: Parameters<typeof StatusBadge>[0]["presentation"] = {
  label: "Nearly orphan",
  description: "Inbound internal links are below the policy threshold.",
  tone: "caution",
  known: true,
};

const BRIDGE: Parameters<typeof StatusBadge>[0]["presentation"] = {
  label: "Bridge page",
  description: "A cut vertex: removing this page would disconnect part of the link graph.",
  tone: "caution",
  known: true,
};

const WELL_LINKED: Parameters<typeof StatusBadge>[0]["presentation"] = {
  label: "Linked",
  description: "Has inbound internal links at or above the policy threshold.",
  tone: "positive",
  known: true,
};

/**
 * Graph diagnostics for one page: degrees, authority, the topical cluster and
 * the component it sits in, plus the orphan / nearly-orphan / bridge flags.
 *
 * `metrics_version` is shown because two versions are never comparable, and
 * `computed_at` because these numbers are a snapshot: the plan's content is
 * immutable but the graph moves under it, so this is read at request time.
 *
 * Numbers only — a table, no chart — so the figures are exact and reachable.
 */
export function GraphSignalsPanel({ signals }: { readonly signals: GraphSignals | null }) {
  if (signals === null) {
    return (
      <p role="status" className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-slate-800">
        No graph metrics for this page. Run <code>make graph-metrics</code> to compute them.
      </p>
    );
  }
  const rows: readonly (readonly [string, string, string])[] = [
    ["Inbound internal links", formatCount(signals.in_degree), "Current internal links pointing at this page."],
    ["Outbound internal links", formatCount(signals.out_degree), "Current internal links from this page."],
    [
      "Internal PageRank",
      formatScore(signals.internal_pagerank),
      "Normalised across the internal graph; the column sums to about 1.",
    ],
    ["Authority score", formatScore(signals.authority_score), "HITS authority: pointed at by good hubs."],
    ["Hub score", formatScore(signals.hub_score), "HITS hub: points at good authorities."],
    [
      "Topical cluster",
      signals.cluster_id === null ? "—" : `#${signals.cluster_id}`,
      "Ordinal of the topical cluster this page was assigned to.",
    ],
    [
      "Connected component",
      signals.component_id === null ? "—" : `#${signals.component_id}`,
      "Ordinal of the weakly connected component this page sits in.",
    ],
    [
      "Crawl depth",
      signals.crawl_depth === null ? "Unreachable" : formatCount(signals.crawl_depth),
      "Clicks from the configured entry page. Unreachable means no path was found.",
    ],
  ];
  return (
    <div className="space-y-3">
      <p className="flex flex-wrap items-center gap-2">
        <StatusBadge
          presentation={signals.is_orphan ? ORPHAN : signals.is_nearly_orphan ? NEARLY_ORPHAN : WELL_LINKED}
        />
        {signals.is_bridge && <StatusBadge presentation={BRIDGE} />}
      </p>
      <TableFrame caption="Link-graph position">
        <thead className="bg-slate-100 text-slate-900">
          <tr>
            <th scope="col" className={`${CELL} font-semibold`}>
              Metric
            </th>
            <th scope="col" className={`${NUM_CELL} font-semibold`}>
              Value
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([term, value, description]) => (
            <tr key={term} className={ROW}>
              <th scope="row" className={`${CELL} font-medium`}>
                {term}
                <span className="block text-xs font-normal text-slate-700">{description}</span>
              </th>
              <td className={NUM_CELL}>{value}</td>
            </tr>
          ))}
        </tbody>
      </TableFrame>
      <p className="text-xs text-slate-700">
        Computed by <code>{signals.metrics_version}</code> at{" "}
        <time dateTime={signals.computed_at}>{formatDateTime(signals.computed_at)}</time> for page version{" "}
        {signals.page_version}. Two metrics versions are never compared.
      </p>
    </div>
  );
}
