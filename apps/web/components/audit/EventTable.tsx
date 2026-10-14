import type { EventRecord } from "@/lib/api-client";
import { describeEventName, formatPayload } from "@/lib/audit";
import { formatDateTime } from "@/lib/format";
import { describeActor } from "@/lib/recommendations";

import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["When", "Event", "Emitted by", "Subject", "Correlation", "Payload"];

/** The append-only event log. Payloads are compact JSON TEXT (sensitive keys withheld), never HTML. */
export function EventTable({ events, total }: { readonly events: readonly EventRecord[]; readonly total: number }) {
  return (
    <TableFrame caption="Events" count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {events.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>No events match.</EmptyRow>
        ) : (
          events.map((event) => {
            const actor = describeActor(event.emitted_by);
            return (
              <tr key={event.id} className={ROW}>
                <td className={`${CELL} whitespace-nowrap`}>
                  <time dateTime={event.occurred_at}>{formatDateTime(event.occurred_at)}</time>
                </td>
                <td className={CELL}>
                  {describeEventName(event.event_name)}
                  <span className="block font-mono text-xs text-slate-600">
                    {event.event_name} v{event.event_version}
                  </span>
                </td>
                <td className={CELL}>
                  <span className="font-medium">{actor.label}</span>{" "}
                  <span className="break-all font-mono text-xs">{actor.id}</span>
                </td>
                <td className={CELL}>
                  {event.subject_type === null && event.subject_id === null ? (
                    <span className="text-slate-600">—</span>
                  ) : (
                    <>
                      <span className="text-xs text-slate-700">{event.subject_type ?? "—"}</span>
                      <span className="block break-all font-mono text-xs">{event.subject_id ?? "—"}</span>
                    </>
                  )}
                </td>
                <td className={CELL}>
                  <span className="break-all font-mono text-xs">{event.correlation_id ?? "—"}</span>
                </td>
                <td className={CELL}>
                  <code className="block max-h-40 max-w-md overflow-auto whitespace-pre-wrap break-all text-xs">
                    {formatPayload(event.payload)}
                  </code>
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
