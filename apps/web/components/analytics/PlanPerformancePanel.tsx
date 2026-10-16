import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { AttributionChain } from "@/components/analytics/AttributionChain";
import { DailyTotalsChart } from "@/components/analytics/DailyTotalsChart";
import { ObservedChangeNote } from "@/components/analytics/ObservedChangeNote";
import { SyntheticBadge, SyntheticBanner } from "@/components/analytics/SyntheticNotice";
import { WindowComparisonCard } from "@/components/analytics/WindowComparisonCard";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "@/components/inventory/Table";
import type { ApiResult, LinkPerformanceDetail } from "@/lib/api-client";
import {
  FIRST_PARTY_CAVEAT,
  describeLiveWindow,
  describeRange,
  describeSource,
  formatCtr,
  hasNoEvents,
  linkSeriesPoints,
  noEventsNote,
  sourcesOf,
} from "@/lib/analytics";
import { formatCount, formatDateTime } from "@/lib/format";

/**
 * The "Performance" panel of a recommendation that was ever on a page: its
 * attribution chain, its live window, and the per-source totals and series
 * (ADR-0015 section 7). Observed data only — nothing here claims a cause.
 */
export function PlanPerformancePanel({ result }: { readonly result: ApiResult<LinkPerformanceDetail> }) {
  if (!result.ok) {
    return <ApiErrorNotice error={result.error} what="Performance" />;
  }
  const detail = result.data;
  const window = describeLiveWindow(detail.live_window);
  const sources = sourcesOf(detail.totals);
  const seriesSources = sourcesOf(detail.series);
  const noEvents = hasNoEvents(detail.totals);
  const hasFirstParty = sources.includes("first_party") || seriesSources.includes("first_party");

  return (
    <div className="space-y-4">
      {detail.synthetic === true && <SyntheticBanner />}

      <div className="flex flex-wrap items-center gap-3">
        <StatusBadge presentation={{ label: window.label, description: window.description, tone: window.tone, known: true }} />
        <span className="text-sm text-slate-700">{window.description}</span>
        <SyntheticBadge synthetic={detail.synthetic} />
      </div>

      <p className="text-sm text-slate-700">Range shown: {describeRange(detail.range)}.</p>

      {window.neverLive ? (
        <p role="status" className="rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-900">
          No data can exist for this recommendation: it was never on a page, so every event for it is rejected as not
          live.
        </p>
      ) : noEvents ? (
        <p role="status" className="rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-900">
          {window.closed
            ? "Its live window has closed and no events were recorded while it was open."
            : noEventsNote(detail.live_window)}
          . Either no visitor saw the paragraph in this range, or no source has imported data for these days yet.
        </p>
      ) : null}

      <TableFrame caption="Totals by source" count={sources.length}>
        <HeaderRow columns={["Source", "Impressions", "Clicks", "CTR"]} />
        <tbody>
          {sources.length === 0 ? (
            <EmptyRow colSpan={4}>No source holds a row for this link in this range.</EmptyRow>
          ) : (
            sources.map((source) => {
              const totals = detail.totals[source]!;
              return (
                <tr key={source} className={ROW}>
                  <th scope="row" className={`${CELL} whitespace-nowrap font-medium`}>
                    {describeSource(source)}
                  </th>
                  <td className={NUM_CELL}>{formatCount(totals.impressions)}</td>
                  <td className={NUM_CELL}>{formatCount(totals.clicks)}</td>
                  <td className={NUM_CELL}>{formatCtr(totals.ctr)}</td>
                </tr>
              );
            })
          )}
        </tbody>
      </TableFrame>

      {hasFirstParty && <p className="max-w-prose text-xs text-slate-700">{FIRST_PARTY_CAVEAT}</p>}

      {seriesSources.map((source) => (
        <section key={source} aria-labelledby={`plan-series-${source}-heading`} className="space-y-2">
          <h3 id={`plan-series-${source}-heading`} className="font-semibold">
            {describeSource(source)} by day
          </h3>
          <DailyTotalsChart
            id={`plan-series-${source}`}
            points={linkSeriesPoints(detail.series[source] ?? [])}
            scopeLabel={describeSource(source)}
            caption={`${describeSource(source)} daily totals for this link`}
          />
        </section>
      ))}

      {detail.measurement !== null && (
        <section aria-labelledby="plan-measurement-heading" className="space-y-1">
          <h3 id="plan-measurement-heading" className="font-semibold">
            Measurement record
          </h3>
          <p className="text-sm text-slate-800">
            Measured{" "}
            <time dateTime={detail.measurement.measured_at}>{formatDateTime(detail.measurement.measured_at)}</time> over{" "}
            <time dateTime={detail.measurement.window_start}>{detail.measurement.window_start}</time> to{" "}
            <time dateTime={detail.measurement.window_end}>{detail.measurement.window_end}</time>, analytics policy
            version {detail.measurement.analytics_policy_version}. The record is kept so this measurement stays
            reproducible after later imports replace their rows.
          </p>
        </section>
      )}

      {(detail.target_impact.length > 0 || detail.source_impact.length > 0) && (
        <section aria-labelledby="plan-impact-heading" className="space-y-3">
          <h3 id="plan-impact-heading" className="font-semibold">
            Observed change around this link
          </h3>
          <ObservedChangeNote />
          {[...detail.target_impact, ...detail.source_impact].map((comparison, index) => (
            <WindowComparisonCard
              key={`${comparison.page_id}:${comparison.source}:${comparison.role}:${index}`}
              id={`plan-impact-${index}`}
              comparison={comparison}
              linkToPlan={false}
              headingLevel={4}
            />
          ))}
        </section>
      )}

      <AttributionChain attribution={detail.attribution} />
    </div>
  );
}
