import { Sparkline } from "@/components/analytics/Sparkline";
import { CELL, HeaderRow, NUM_CELL, ROW, TableFrame } from "@/components/inventory/Table";
import type { AnalyticsSource, PageDailyPoint } from "@/lib/api-client";
import { describeComparisonMetric, describeSource, formatPosition } from "@/lib/analytics";
import { formatCount } from "@/lib/format";

/** The metric a page source is compared on (ADR-0015 section 12.9.6). */
export function pageMetricOf(source: string): "clicks" | "pageviews" {
  return source === "search_console" ? "clicks" : "pageviews";
}

export interface PageSeriesPanelProps {
  /** Unique within the page. */
  readonly id: string;
  readonly source: AnalyticsSource | string;
  readonly points: readonly PageDailyPoint[];
}

/**
 * One page source's daily rows: a sparkline of the metric that source is
 * compared on, with every stored number in the table beneath it.
 */
export function PageSeriesPanel({ id, source, points }: PageSeriesPanelProps) {
  const metric = pageMetricOf(source);
  const metricLabel = describeComparisonMetric(metric);
  const sourceLabel = describeSource(source);
  const sorted = [...points].sort((a, b) => a.metric_date.localeCompare(b.metric_date, "en"));
  if (sorted.length === 0) {
    return (
      <p role="status" className="rounded border border-slate-300 bg-slate-50 p-3 text-slate-800">
        No {sourceLabel} rows are stored for this window.
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <figure className="space-y-1 rounded border border-slate-300 p-3">
        <figcaption className="text-sm font-medium text-slate-800">
          {sourceLabel}: {metricLabel.toLowerCase()} per day
        </figcaption>
        <Sparkline
          id={id}
          points={sorted.map((point) => ({ date: point.metric_date, value: point[metric] }))}
          label={`${sourceLabel} ${metricLabel.toLowerCase()} per day`}
        />
      </figure>
      <TableFrame caption={`${sourceLabel} daily rows`} count={sorted.length}>
        <HeaderRow columns={["Day", "Impressions", "Clicks", "Pageviews", "Sessions", "Average position"]} />
        <tbody>
          {sorted.map((point) => (
            <tr key={point.metric_date} className={ROW}>
              <th scope="row" className={`${CELL} whitespace-nowrap font-medium`}>
                <time dateTime={point.metric_date}>{point.metric_date}</time>
              </th>
              <td className={NUM_CELL}>{formatCount(point.impressions)}</td>
              <td className={NUM_CELL}>{formatCount(point.clicks)}</td>
              <td className={NUM_CELL}>{formatCount(point.pageviews)}</td>
              <td className={NUM_CELL}>{formatCount(point.sessions)}</td>
              <td className={NUM_CELL}>{formatPosition(point.avg_position)}</td>
            </tr>
          ))}
        </tbody>
      </TableFrame>
    </div>
  );
}
