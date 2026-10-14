import type { LinkPlan } from "@/lib/api-client";
import { formatScore } from "@/lib/intelligence";
import { describeConfidence } from "@/lib/recommendations";

/**
 * A plan's total score with its confidence and an explicit calibration
 * qualifier. Raw confidence is always labelled "raw" — never as calibrated.
 */
export function PlanScore({ plan }: { readonly plan: Pick<LinkPlan, "score" | "confidence" | "calibration_version"> }) {
  const confidence = describeConfidence(plan);
  return (
    <span className="inline-flex flex-col gap-0.5">
      <span className="font-medium tabular-nums">{formatScore(plan.score.total)}</span>
      <span className="whitespace-nowrap text-xs text-slate-700" title={confidence.description}>
        confidence <span className="tabular-nums">{confidence.value}</span>{" "}
        <span
          className={`rounded border px-1 font-medium ${
            confidence.calibrated ? "border-emerald-700 text-emerald-900" : "border-amber-700 text-amber-950"
          }`}
        >
          {confidence.qualifier}
        </span>
        <span className="sr-only"> ({confidence.description})</span>
      </span>
    </span>
  );
}
