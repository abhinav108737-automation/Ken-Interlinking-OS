import { OBSERVED_CHANGE_NOTE, OBSERVED_CHANGE_TITLE } from "@/lib/analytics";

/**
 * The fixed, prominent note that accompanies every before/after comparison.
 * The wording is binding (ADR-0015 sections 3, 7 and 12): the console never
 * claims causality, significance, ranking impact or revenue.
 */
export function ObservedChangeNote() {
  return (
    <div role="note" aria-label={OBSERVED_CHANGE_TITLE} className="rounded border-2 border-slate-500 bg-slate-50 p-3">
      <p className="font-semibold text-slate-900">{OBSERVED_CHANGE_TITLE}</p>
      <p className="max-w-prose text-sm text-slate-800">{OBSERVED_CHANGE_NOTE}</p>
    </div>
  );
}
