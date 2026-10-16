import type { MetricTotals } from "@/lib/api-client";
import { formatCtr } from "@/lib/analytics";
import { formatCount } from "@/lib/format";

export interface KpiTilesProps {
  /** Plans whose live window is open now (not range-dependent). */
  readonly liveLinks: number;
  /** Plans with any link-level row in the range. */
  readonly linksWithData: number;
  readonly totals: MetricTotals;
  /** What the impression/click/CTR tiles cover, e.g. "GA4" or "all sources". */
  readonly scopeLabel: string;
}

function Tile({
  term,
  value,
  note,
}: {
  readonly term: string;
  readonly value: string;
  readonly note: string;
}) {
  return (
    <div className="rounded border border-slate-300 bg-white p-3">
      <dt className="text-sm font-medium text-slate-700">{term}</dt>
      <dd className="text-2xl font-semibold tabular-nums text-slate-900">{value}</dd>
      <p className="mt-1 text-xs text-slate-700">{note}</p>
    </div>
  );
}

/**
 * The four Phase 7 key figures. Landing events were cut (ADR-0015 section 12.2),
 * so there is no "landings" tile. CTR shows an em dash when there are no
 * impressions — never 0 %.
 */
export function KpiTiles({ liveLinks, linksWithData, totals, scopeLabel }: KpiTilesProps) {
  return (
    <dl aria-label="Key figures" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Tile
        term="Live links"
        value={formatCount(liveLinks)}
        note={`Deployed links on a page right now. ${formatCount(linksWithData)} have data in this range.`}
      />
      <Tile term="Impressions" value={formatCount(totals.impressions)} note={`Link impressions in range (${scopeLabel}).`} />
      <Tile term="Clicks" value={formatCount(totals.clicks)} note={`Clicks on those links in range (${scopeLabel}).`} />
      <Tile
        term="CTR"
        value={formatCtr(totals.ctr)}
        note={
          totals.ctr === null
            ? "No impressions in range, so there is no rate to show."
            : `Clicks divided by impressions (${scopeLabel}).`
        }
      />
    </dl>
  );
}
