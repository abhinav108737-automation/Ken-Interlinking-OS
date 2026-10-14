import Link from "next/link";

import type { AgentRunSummary } from "@/lib/api-client";
import {
  agentRunDuration,
  agentRunHref,
  describeAgentOutcome,
  describeAgentRunStatus,
  describeAgentTrigger,
  describeTriageDecision,
} from "@/lib/agents";
import { formatCount, formatDateTime } from "@/lib/format";

import { StatusBadge } from "../StatusBadge";
import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Started", "Source page", "Trigger", "Status", "Outcome", "Triage", "Handed off", "Duration"];
const LINK = "text-blue-800 underline underline-offset-2";

/** Agent run history, newest first (API order). Expects a non-empty list; the screen renders the empty state. */
export function AgentRunTable({ runs, total }: { readonly runs: readonly AgentRunSummary[]; readonly total: number }) {
  return (
    <TableFrame caption="Agent runs" count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {runs.map((run) => (
          <tr key={run.id} className={ROW}>
            <th scope="row" className={`${CELL} whitespace-nowrap font-medium`}>
              <Link href={agentRunHref(run.id)} className={LINK}>
                <time dateTime={run.started_at}>{formatDateTime(run.started_at)}</time>{" "}
                <span className="sr-only">— view agent run</span>
              </Link>
            </th>
            <td className={CELL}>
              <Link href={pageHref(run.source_page_id)} className={`${LINK} break-all font-mono text-xs`}>
                {run.source_url}
              </Link>
              {run.source_page_version !== null && (
                <span className="block text-xs text-slate-600">version {run.source_page_version}</span>
              )}
            </td>
            <td className={CELL}>{describeAgentTrigger(run.trigger)}</td>
            <td className={CELL}>
              <StatusBadge presentation={describeAgentRunStatus(run.status)} />
            </td>
            <td className={CELL}>
              <StatusBadge presentation={describeAgentOutcome(run.outcome)} />
            </td>
            <td className={CELL}>
              {run.triage_decision === null ? (
                <span className="text-slate-600">—</span>
              ) : (
                describeTriageDecision(run.triage_decision)
              )}
            </td>
            <td className={NUM_CELL}>{formatCount(run.handed_off_plan_ids.length)}</td>
            <td className={`${CELL} whitespace-nowrap`}>{agentRunDuration(run)}</td>
          </tr>
        ))}
      </tbody>
    </TableFrame>
  );
}
