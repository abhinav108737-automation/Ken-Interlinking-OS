import { CELL, HeaderRow, NUM_CELL, ROW, TableFrame } from "@/components/inventory/Table";
import { Sparkline, type SparkPoint } from "@/components/analytics/Sparkline";
import type { SeriesPoint } from "@/lib/analytics";
import { formatCount } from "@/lib/format";

export interface DailyTotalsChartProps {
  /** Unique within the page. */
  readonly id: string;
  readonly points: readonly SeriesPoint[];
  /** What the series covers, e.g. "all sources" or "GA4". */
  readonly scopeLabel: string;
  readonly caption: string;
}

function toSpark(points: readonly SeriesPoint[], key: "impressions" | "clicks"): SparkPoint[] {
  return points.map((point) => ({ date: point.date, value: point[key] }));
}

/**
 * Two sparklines over the same days, with the exact numbers in a table beneath
 * them. The table is the primary rendering: the drawing adds shape, never data.
 */
export function DailyTotalsChart({ id, points, scopeLabel, caption }: DailyTotalsChartProps) {
  if (points.length === 0) {
    return (
      <p role="status" className="rounded border border-slate-300 bg-slate-50 p-3 text-slate-800">
        No daily rows are stored for this range ({scopeLabel}).
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <figure className="space-y-2 rounded border border-slate-300 p-3">
        <figcaption className="text-sm font-medium text-slate-800">
          {caption} — {scopeLabel}
        </figcaption>
        <div className="space-y-1">
          <p className="text-xs font-medium text-slate-700">Impressions per day</p>
          <Sparkline id={`${id}-impressions`} points={toSpark(points, "impressions")} label="Impressions per day" />
        </div>
        <div className="space-y-1">
          <p className="text-xs font-medium text-slate-700">Clicks per day</p>
          <Sparkline id={`${id}-clicks`} points={toSpark(points, "clicks")} label="Clicks per day" />
        </div>
      </figure>
      <TableFrame caption={`${caption} (daily values)`} count={points.length}>
        <HeaderRow columns={["Day", "Impressions", "Clicks"]} />
        <tbody>
          {points.map((point) => (
            <tr key={point.date} className={ROW}>
              <th scope="row" className={`${CELL} whitespace-nowrap font-medium`}>
                <time dateTime={point.date}>{point.date}</time>
              </th>
              <td className={NUM_CELL}>{formatCount(point.impressions)}</td>
              <td className={NUM_CELL}>{formatCount(point.clicks)}</td>
            </tr>
          ))}
        </tbody>
      </TableFrame>
    </div>
  );
}
