import { describeConfounderFlag } from "@/lib/analytics";
import type { Tone } from "@/lib/status";

const TONE: Readonly<Record<Tone, string>> = {
  positive: "border-emerald-700 bg-emerald-50 text-emerald-900",
  caution: "border-amber-700 bg-amber-50 text-amber-950",
  critical: "border-red-700 bg-red-50 text-red-900",
  neutral: "border-slate-500 bg-slate-100 text-slate-900",
};

/**
 * The confounders the API flagged on one before/after comparison. They are
 * reasons for caution, never corrections: no flag changes a number.
 */
export function ConfounderChips({
  flags,
  label = "Confounders",
}: {
  readonly flags: readonly string[];
  readonly label?: string;
}) {
  if (flags.length === 0) {
    return <p className="text-sm text-slate-700">No confounder was flagged. Other causes are still possible.</p>;
  }
  return (
    <ul aria-label={label} className="flex flex-wrap gap-2">
      {flags.map((flag) => {
        const presentation = describeConfounderFlag(flag);
        return (
          <li
            key={flag}
            className={`rounded border px-2 py-0.5 text-xs font-medium ${TONE[presentation.tone]}`}
            title={presentation.description}
          >
            {presentation.label}
          </li>
        );
      })}
    </ul>
  );
}
