import Link from "next/link";

import type { AgentPlanReview } from "@/lib/api-client";
import {
  FALLBACK_PRESENTATION,
  agentRunHref,
  describeCriticReason,
  describeCriticVerdict,
  describeFallbackReason,
} from "@/lib/agents";
import { formatDateTime } from "@/lib/format";
import { planHref } from "@/lib/recommendations";

import { StatusBadge } from "../StatusBadge";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "../inventory/Table";

/** "Fallback" badge plus its reason, or the model that answered. All text. */
export function ModelUse({
  fallbackUsed,
  fallbackReason,
  model,
}: {
  readonly fallbackUsed: boolean;
  readonly fallbackReason: string | null;
  readonly model: string | null;
}) {
  return (
    <div className="space-y-1">
      {fallbackUsed && (
        <p className="flex flex-wrap items-center gap-1">
          <StatusBadge presentation={FALLBACK_PRESENTATION} />
          <span className="text-xs">
            {fallbackReason === null ? "No reason recorded" : describeFallbackReason(fallbackReason)}
          </span>
        </p>
      )}
      {model === null || model.trim() === "" ? (
        <p className="text-xs text-slate-600">{fallbackUsed ? "No model answered" : "No model"}</p>
      ) : (
        <p>
          <code className="break-all text-xs">{model}</code>
        </p>
      )}
    </div>
  );
}

/**
 * Critic reviews of plans. The explanation is MODEL OUTPUT over untrusted
 * corpus text (ADR-0012 section 6): it is rendered strictly as a React text
 * node, never as HTML.
 *
 * `showPlan` adds a column linking each review to its plan (the run screen);
 * the plan's own screen omits it.
 */
export function AgentReviewTable({
  reviews,
  caption,
  emptyMessage,
  showPlan = true,
}: {
  readonly reviews: readonly AgentPlanReview[];
  readonly caption: string;
  readonly emptyMessage: string;
  readonly showPlan?: boolean;
}) {
  const columns = [
    ...(showPlan ? ["Plan"] : []),
    "Verdict",
    "Reasons",
    "Explanation",
    "Handed off",
    "Model",
    "Reviewed",
  ];
  return (
    <TableFrame caption={caption} count={reviews.length}>
      <HeaderRow columns={columns} />
      <tbody>
        {reviews.length === 0 ? (
          <EmptyRow colSpan={columns.length}>{emptyMessage}</EmptyRow>
        ) : (
          reviews.map((review) => (
            <tr key={review.plan_id} className={ROW}>
              {showPlan && (
                <th scope="row" className={`${CELL} font-medium`}>
                  <Link
                    href={planHref(review.plan_id)}
                    className="break-all font-mono text-xs text-blue-800 underline underline-offset-2"
                  >
                    {review.plan_id}
                  </Link>
                </th>
              )}
              <td className={CELL}>
                <StatusBadge presentation={describeCriticVerdict(review.verdict)} />
              </td>
              <td className={CELL}>
                {review.reasons.length === 0 ? (
                  <span className="text-slate-600">—</span>
                ) : (
                  <ul className="space-y-0.5 text-xs">
                    {review.reasons.map((reason) => (
                      <li key={reason}>{describeCriticReason(reason)}</li>
                    ))}
                  </ul>
                )}
              </td>
              <td className={`${CELL} max-w-sm`}>
                {review.explanation.trim() === "" ? (
                  <span className="text-slate-600">No explanation</span>
                ) : (
                  <p className="whitespace-pre-wrap break-words">{review.explanation}</p>
                )}
              </td>
              <td className={CELL}>
                {review.handed_off ? (
                  <span className="font-medium">Yes — sent to human review</span>
                ) : (
                  <span>No — left as proposed</span>
                )}
              </td>
              <td className={CELL}>
                <ModelUse
                  fallbackUsed={review.fallback_used}
                  fallbackReason={review.fallback_reason}
                  model={review.model}
                />
              </td>
              <td className={`${CELL} whitespace-nowrap`}>
                <Link
                  href={agentRunHref(review.run_id)}
                  className="text-blue-800 underline underline-offset-2"
                >
                  <time dateTime={review.reviewed_at}>{formatDateTime(review.reviewed_at)}</time>
                  <span className="sr-only"> — view agent run</span>
                </Link>
              </td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
