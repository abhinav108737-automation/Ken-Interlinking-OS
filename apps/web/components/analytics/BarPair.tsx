import { formatMean } from "@/lib/analytics";

export interface BarPairProps {
  /** Unique within the page: the `<title>` is referenced by this id. */
  readonly id: string;
  /** Per-day mean of the window before the change; `null` when it cannot be computed. */
  readonly pre: number | null;
  /** Per-day mean of the window after the change; `null` when it cannot be computed. */
  readonly post: number | null;
  /** What the bars measure, e.g. "Clicks per day". */
  readonly label: string;
}

const WIDTH = 240;
const HEIGHT = 96;
const PAD = 8;
const BAR = 64;

/** The sentence a screen reader hears instead of the drawing. */
export function barPairSummary(pre: number | null, post: number | null, label: string): string {
  return (
    `${label}: before ${formatMean(pre)}, after ${formatMean(post)}. ` +
    "This is an observed change, not a causal effect. The exact values are in the table below."
  );
}

/**
 * Two hand-written SVG bars: the per-day mean before and after a link was
 * applied. Deliberately unlabelled inside the drawing — the table beside it
 * carries the numbers, the windows and the confounders.
 */
export function BarPair({ id, pre, post, label }: BarPairProps) {
  const max = Math.max(pre ?? 0, post ?? 0, 1);
  const titleId = `${id}-title`;
  const baseline = HEIGHT - PAD;
  const height = (value: number | null) => (value === null ? 0 : ((value / max) * (HEIGHT - 2 * PAD)) || 0);
  const bars = [
    { key: "pre", x: PAD + 16, value: pre, fill: "#94a3b8" },
    { key: "post", x: PAD + 16 + BAR + 24, value: post, fill: "#1e40af" },
  ];
  return (
    <svg
      role="img"
      aria-labelledby={titleId}
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      className="h-24 w-60"
      focusable="false"
    >
      <title id={titleId}>{barPairSummary(pre, post, label)}</title>
      <line x1={PAD} y1={baseline} x2={WIDTH - PAD} y2={baseline} stroke="#cbd5e1" strokeWidth="1" />
      {bars.map((bar) => {
        const barHeight = height(bar.value);
        return barHeight <= 0 ? null : (
          <rect key={bar.key} x={bar.x} y={baseline - barHeight} width={BAR} height={barHeight} fill={bar.fill} />
        );
      })}
    </svg>
  );
}
