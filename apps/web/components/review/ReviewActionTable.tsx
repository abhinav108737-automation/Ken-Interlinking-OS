import Link from "next/link";

import type { ReviewActionView } from "@/lib/api-client";
import { formatDateTime } from "@/lib/format";
import { describeActor, describeLinkPlanStatus, planHref } from "@/lib/recommendations";
import { describeFeedbackReason, describeReviewAction } from "@/lib/review";

import { StatusBadge } from "../StatusBadge";
import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "../inventory/Table";

const LINK = "text-blue-800 underline underline-offset-2";

export interface ReviewActionTableProps {
  readonly actions: readonly ReviewActionView[];
  readonly caption: string;
  readonly total?: number;
  readonly emptyMessage?: string;
  /** Show the plan column (audit log, dashboard); off on a plan's own history. */
  readonly showPlan?: boolean;
}

function Change({ before, after, href }: { readonly before: string; readonly after: string | null; readonly href?: (value: string) => string }) {
  if (after === null) {
    return href === undefined ? <span className="break-words">{before}</span> : <Link href={href(before)} className={`${LINK} break-all font-mono text-xs`}>{before}</Link>;
  }
  return (
    <span className="block space-y-0.5">
      <span className="block">
        <span className="text-xs text-slate-700">Before: </span>
        {href === undefined ? <span className="break-words">{before}</span> : <Link href={href(before)} className={`${LINK} break-all font-mono text-xs`}>{before}</Link>}
      </span>
      <span className="block">
        <span className="text-xs text-slate-700">After: </span>
        {href === undefined ? <span className="break-words font-medium">{after}</span> : <Link href={href(after)} className={`${LINK} break-all font-mono text-xs`}>{after}</Link>}
      </span>
    </span>
  );
}

/**
 * Human review actions (the audit AND editorial-feedback record). Notes,
 * anchors and actors are rendered as React text — never HTML.
 */
export function ReviewActionTable({ actions, caption, total, emptyMessage = "No review actions yet.", showPlan = true }: ReviewActionTableProps) {
  const columns = ["When", "Reviewer", "Action", ...(showPlan ? ["Recommendation"] : []), "Status", "Reasons and note", "Anchor", "Destination"];
  return (
    <TableFrame caption={caption} {...(total === undefined ? {} : { count: total })}>
      <HeaderRow columns={columns} />
      <tbody>
        {actions.length === 0 ? (
          <EmptyRow colSpan={columns.length}>{emptyMessage}</EmptyRow>
        ) : (
          actions.map((row) => {
            const actor = describeActor(row.actor);
            return (
              <tr key={row.id} className={ROW}>
                <td className={`${CELL} whitespace-nowrap`}>
                  <time dateTime={row.created_at}>{formatDateTime(row.created_at)}</time>
                </td>
                <td className={CELL}>
                  <span className="font-medium">{actor.label}</span>{" "}
                  <span className="break-all font-mono text-xs">{actor.id}</span>
                </td>
                <td className={CELL}>
                  {describeReviewAction(row.action)}
                  {row.bulk_operation_id !== null && <span className="block text-xs text-slate-700">(bulk)</span>}
                </td>
                {showPlan && (
                  <td className={CELL}>
                    <Link href={planHref(row.plan_id)} className={`${LINK} break-all font-mono text-xs`}>
                      {row.plan_id}
                    </Link>
                    {row.new_plan_id !== null && (
                      <span className="block text-xs">
                        New:{" "}
                        <Link href={planHref(row.new_plan_id)} className={`${LINK} break-all font-mono`}>
                          {row.new_plan_id}
                        </Link>
                      </span>
                    )}
                  </td>
                )}
                <td className={CELL}>
                  <span className="flex flex-wrap items-center gap-1">
                    <StatusBadge presentation={describeLinkPlanStatus(row.from_status)} />
                    <span aria-hidden="true">→</span>
                    <span className="sr-only">to</span>
                    <StatusBadge presentation={describeLinkPlanStatus(row.to_status)} />
                  </span>
                  {row.new_plan_status !== null && (
                    <span className="mt-1 block text-xs text-slate-700">
                      New plan: {describeLinkPlanStatus(row.new_plan_status).label}
                    </span>
                  )}
                  {!showPlan && row.new_plan_id !== null && (
                    <Link href={planHref(row.new_plan_id)} className={`${LINK} mt-1 block text-xs`}>
                      View the new recommendation
                    </Link>
                  )}
                </td>
                <td className={CELL}>
                  {row.reasons.length === 0 ? (
                    <span className="text-slate-600">No reason given</span>
                  ) : (
                    <ul className="list-disc space-y-0.5 pl-4 text-xs">
                      {row.reasons.map((reason) => (
                        <li key={reason}>{describeFeedbackReason(reason)}</li>
                      ))}
                    </ul>
                  )}
                  {row.note.trim() !== "" && <p className="mt-1 whitespace-pre-wrap break-words text-sm">{row.note}</p>}
                </td>
                <td className={CELL}>
                  <Change before={row.anchor_before} after={row.anchor_after} />
                </td>
                <td className={CELL}>
                  <Change before={row.target_before} after={row.target_after} href={pageHref} />
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
