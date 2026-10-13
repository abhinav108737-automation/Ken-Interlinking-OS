import type { StatusPresentation, Tone } from "@/lib/status";

// Colour is additive only: the label text always carries the meaning, and each
// tone also has a distinct glyph so it survives greyscale and colour-blindness.
const TONE_CLASSES: Readonly<Record<Tone, string>> = {
  positive: "border-emerald-700 bg-emerald-50 text-emerald-900",
  caution: "border-amber-700 bg-amber-50 text-amber-950",
  critical: "border-red-700 bg-red-50 text-red-900",
  neutral: "border-slate-500 bg-slate-100 text-slate-900",
};

const TONE_GLYPH: Readonly<Record<Tone, string>> = {
  positive: "✓",
  caution: "!",
  critical: "✕",
  neutral: "–",
};

export interface StatusBadgeProps {
  readonly presentation: StatusPresentation;
}

export function StatusBadge({ presentation }: StatusBadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-sm font-medium ${TONE_CLASSES[presentation.tone]}`}
      title={presentation.description}
    >
      <span aria-hidden="true">{TONE_GLYPH[presentation.tone]}</span>
      <span>{presentation.label}</span>
    </span>
  );
}
