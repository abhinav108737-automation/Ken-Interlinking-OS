import Link from "next/link";

import { BarPair } from "@/components/analytics/BarPair";
import { ConfounderChips } from "@/components/analytics/ConfounderChips";
import { SyntheticBadge } from "@/components/analytics/SyntheticNotice";
import { CELL, HeaderRow, NUM_CELL, ROW, TableFrame } from "@/components/inventory/Table";
import type { PageWindowComparison, WindowStats } from "@/lib/api-client";
import {
  comparisonCoverage,
  describeComparisonMetric,
  describeComparisonRole,
  describeSource,
  formatCtr,
  formatDelta,
  formatMean,
  formatPosition,
  formatRatio,
  windowTotal,
} from "@/lib/analytics";
import { formatCount } from "@/lib/format";
import { planHref } from "@/lib/recommendations";

export interface WindowComparisonCardProps {
  /** Unique within the page. */
  readonly id: string;
  readonly comparison: PageWindowComparison;
  /** Link the heading to the recommendation (off when the card is already on its page). */
  readonly linkToPlan?: boolean;
  /** The card's heading level, so it fits the section it is rendered in. */
  readonly headingLevel?: 3 | 4;
}

function windowDates(stats: WindowStats): string {
  return stats.end === null ? `${stats.start} (empty)` : `${stats.start} to ${stats.end}`;
}

/**
 * One page's before/after comparison around one link change: the paired bars,
 * the exact numbers, the confounders and — when a window is too short — why no
 * change is shown. Every number is an OBSERVED change; the fixed note that says
 * so is rendered once per screen by `ObservedChangeNote`.
 */
export function WindowComparisonCard({ id, comparison, linkToPlan = true, headingLevel = 3 }: WindowComparisonCardProps) {
  const Heading = headingLevel === 4 ? "h4" : "h3";
  const metric = describeComparisonMetric(comparison.metric);
  const coverage = comparisonCoverage(comparison);
  const isSearch = comparison.source === "search_console";
  const rows: { readonly term: string; readonly pre: string; readonly post: string }[] = [
    { term: "Window", pre: windowDates(comparison.pre), post: windowDates(comparison.post) },
    {
      term: "Days with data",
      pre: `${formatCount(comparison.pre.covered_days)} of ${formatCount(comparison.pre.days)}`,
      post: `${formatCount(comparison.post.covered_days)} of ${formatCount(comparison.post.days)}`,
    },
    {
      term: `${metric} (total)`,
      pre: formatCount(windowTotal(comparison.pre, comparison.metric)),
      post: formatCount(windowTotal(comparison.post, comparison.metric)),
    },
    {
      term: `${metric} per day with data`,
      pre: formatMean(comparison.pre.mean_per_day),
      post: formatMean(comparison.post.mean_per_day),
    },
    ...(isSearch
      ? [
          { term: "Impressions (total)", pre: formatCount(comparison.pre.impressions), post: formatCount(comparison.post.impressions) },
          { term: "CTR", pre: formatCtr(comparison.pre.ctr), post: formatCtr(comparison.post.ctr) },
          {
            term: "Average position",
            pre: formatPosition(comparison.pre.avg_position),
            post: formatPosition(comparison.post.avg_position),
          },
        ]
      : [{ term: "Sessions (total)", pre: formatCount(comparison.pre.sessions), post: formatCount(comparison.post.sessions) }]),
  ];

  return (
    <article aria-labelledby={`${id}-heading`} className="space-y-3 rounded border border-slate-300 p-3">
      <header className="space-y-1">
        <Heading id={`${id}-heading`} className="font-semibold">
          {describeSource(comparison.source)}: {metric} around{" "}
          <time dateTime={comparison.anchor_date}>{comparison.anchor_date}</time>{" "}
          <SyntheticBadge synthetic={comparison.synthetic} />
        </Heading>
        <p className="text-sm text-slate-700">
          {describeComparisonRole(comparison.role)}. The day the link went live is left out as a partial day.{" "}
          {linkToPlan && (
            <Link href={planHref(comparison.plan_id)} className="text-blue-800 underline underline-offset-2">
              View the recommendation
            </Link>
          )}
        </p>
      </header>

      <div className="flex flex-wrap items-start gap-4">
        <figure className="space-y-1">
          <BarPair
            id={id}
            pre={comparison.pre.mean_per_day}
            post={comparison.post.mean_per_day}
            label={`${metric} per day with data`}
          />
          <figcaption className="text-xs text-slate-700">
            Left: before ({formatMean(comparison.pre.mean_per_day)}). Right: after ({formatMean(comparison.post.mean_per_day)}).
          </figcaption>
        </figure>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="font-medium text-slate-700">Change per day</dt>
          <dd className="tabular-nums text-slate-900">
            {coverage.comparable ? formatDelta(comparison.delta_per_day) : "Not shown"}
          </dd>
          <dt className="font-medium text-slate-700">Ratio</dt>
          <dd className="tabular-nums text-slate-900">{coverage.comparable ? formatRatio(comparison.ratio) : "Not shown"}</dd>
          <dt className="font-medium text-slate-700">Site trend (context only)</dt>
          <dd className="tabular-nums text-slate-900">{formatRatio(comparison.site_trend_ratio)}</dd>
        </dl>
      </div>

      {!coverage.comparable && (
        <p role="status" className="rounded border border-slate-400 bg-slate-50 p-2 text-sm text-slate-900">
          <span aria-hidden="true">! </span>
          No change is shown: a window has too few days with stored data.
          {coverage.notes.length > 0 && ` ${coverage.notes.join(". ")}.`}
        </p>
      )}

      <TableFrame caption={`Before and after, ${describeSource(comparison.source)}`}>
        <HeaderRow columns={["Measure", "Before", "After"]} />
        <tbody>
          {rows.map((row) => (
            <tr key={row.term} className={ROW}>
              <th scope="row" className={`${CELL} font-medium`}>
                {row.term}
              </th>
              <td className={NUM_CELL}>{row.pre}</td>
              <td className={NUM_CELL}>{row.post}</td>
            </tr>
          ))}
        </tbody>
      </TableFrame>

      <ConfounderChips flags={comparison.flags} label={`Confounders for ${describeSource(comparison.source)}`} />
    </article>
  );
}
