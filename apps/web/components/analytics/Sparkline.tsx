import { formatCount } from "@/lib/format";

/** One point of a daily series. The date is `YYYY-MM-DD`. */
export interface SparkPoint {
  readonly date: string;
  readonly value: number;
}

export interface SparklineProps {
  /** Unique within the page: the `<title>` is referenced by this id. */
  readonly id: string;
  readonly points: readonly SparkPoint[];
  /** What the line shows, e.g. "Clicks per day". Becomes the accessible name. */
  readonly label: string;
}

const WIDTH = 640;
const HEIGHT = 80;
const PAD = 6;

function geometry(points: readonly SparkPoint[]): { readonly x: number; readonly y: number }[] {
  const max = Math.max(...points.map((point) => point.value), 1);
  const span = Math.max(points.length - 1, 1);
  return points.map((point, index) => ({
    x: PAD + (index / span) * (WIDTH - 2 * PAD),
    y: HEIGHT - PAD - (point.value / max) * (HEIGHT - 2 * PAD),
  }));
}

/** The sentence a screen reader hears instead of the drawing. */
export function sparklineSummary(points: readonly SparkPoint[], label: string): string {
  if (points.length === 0) return `${label}: no data.`;
  const values = points.map((point) => point.value);
  const total = values.reduce((sum, value) => sum + value, 0);
  const highest = Math.max(...values);
  const peak = points[values.indexOf(highest)]!;
  const first = points[0]!;
  const last = points[points.length - 1]!;
  return (
    `${label}: ${formatCount(points.length)} days from ${first.date} to ${last.date}. ` +
    `Total ${formatCount(total)}, highest ${formatCount(highest)} on ${peak.date}, lowest ${formatCount(Math.min(...values))}. ` +
    "The exact values are in the table below."
  );
}

/**
 * A hand-written SVG sparkline: no chart dependency, no script, no interaction.
 * It is a picture of data that is also present as text in the table beside it,
 * so the drawing itself carries no information of its own.
 */
export function Sparkline({ id, points, label }: SparklineProps) {
  if (points.length === 0) return null;
  const coordinates = geometry(points);
  const titleId = `${id}-title`;
  const baseline = HEIGHT - PAD;
  return (
    <svg
      role="img"
      aria-labelledby={titleId}
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      className="h-20 w-full"
      focusable="false"
    >
      <title id={titleId}>{sparklineSummary(points, label)}</title>
      <line x1={PAD} y1={baseline} x2={WIDTH - PAD} y2={baseline} stroke="#cbd5e1" strokeWidth="1" />
      {coordinates.length === 1 ? (
        <circle cx={coordinates[0]!.x} cy={coordinates[0]!.y} r="3.5" fill="#1e40af" />
      ) : (
        <polyline
          points={coordinates.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" ")}
          fill="none"
          stroke="#1e40af"
          strokeWidth="2"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      )}
      {coordinates.length > 1 && (
        <circle cx={coordinates[coordinates.length - 1]!.x} cy={coordinates[coordinates.length - 1]!.y} r="3" fill="#1e40af" />
      )}
    </svg>
  );
}
