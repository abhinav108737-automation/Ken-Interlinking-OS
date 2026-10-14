import type { PlanTransitionView } from "@/lib/api-client";
import { formatDateTime } from "@/lib/format";
import { describeActor, describeLinkPlanStatus } from "@/lib/recommendations";

import { StatusBadge } from "../StatusBadge";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["When", "From", "To", "Actor", "Note"];

/** The append-only status history of one plan, oldest first (API order). Actors and notes are text. */
export function TransitionHistory({ transitions }: { readonly transitions: readonly PlanTransitionView[] }) {
  return (
    <TableFrame caption="Status history" count={transitions.length}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {transitions.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>No status transitions are recorded for this plan.</EmptyRow>
        ) : (
          transitions.map((transition) => {
            const actor = describeActor(transition.actor);
            return (
              <tr key={transition.id} className={ROW}>
                <td className={`${CELL} whitespace-nowrap`}>
                  <time dateTime={transition.occurred_at}>{formatDateTime(transition.occurred_at)}</time>
                </td>
                <td className={CELL}>
                  {transition.from_status === null ? (
                    <span className="text-slate-600">(created)</span>
                  ) : (
                    <StatusBadge presentation={describeLinkPlanStatus(transition.from_status)} />
                  )}
                </td>
                <td className={CELL}>
                  <span className="sr-only">to </span>
                  <StatusBadge presentation={describeLinkPlanStatus(transition.to_status)} />
                </td>
                <td className={CELL}>
                  <span className="font-medium">{actor.label}</span>{" "}
                  <span className="break-all font-mono text-xs">{actor.id}</span>
                </td>
                <td className={CELL}>
                  {transition.note === null || transition.note.trim() === "" ? (
                    <span className="text-slate-600">—</span>
                  ) : (
                    <span className="whitespace-pre-wrap break-words">{transition.note}</span>
                  )}
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
