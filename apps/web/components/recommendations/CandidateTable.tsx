import Link from "next/link";

import type { CandidateView } from "@/lib/api-client";
import { formatScore } from "@/lib/intelligence";
import { describeCandidateRejection, describeRetrievalChannel, planHref } from "@/lib/recommendations";
import type { StatusPresentation } from "@/lib/status";

import { StatusBadge } from "../StatusBadge";
import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Target page", "Channels", "Total score", "Outcome"];

const KEPT: StatusPresentation = {
  label: "Kept",
  description: "Passed every filter and became a plan.",
  tone: "positive",
  known: true,
};

const REJECTED: StatusPresentation = {
  label: "Rejected",
  description: "Removed by a deterministic filter; the first matching rule is recorded.",
  tone: "neutral",
  known: true,
};

/** Retrieved candidates, kept or rejected, with the first rejection rule (ADR-0011 §3). */
export function CandidateTable({
  candidates,
  total,
  emptyMessage,
}: {
  readonly candidates: readonly CandidateView[];
  readonly total: number;
  readonly emptyMessage: string;
}) {
  return (
    <TableFrame caption="Candidates" count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {candidates.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>{emptyMessage}</EmptyRow>
        ) : (
          candidates.map((candidate) => (
            <tr key={`${candidate.run_id}:${candidate.source_page_id}:${candidate.target_page_id}`} className={ROW}>
              <th scope="row" className={`${CELL} font-medium`}>
                <Link
                  href={pageHref(candidate.target_page_id)}
                  className="break-all font-mono text-xs text-blue-800 underline underline-offset-2"
                >
                  {candidate.target_url}
                </Link>
              </th>
              <td className={CELL}>
                <ul className="space-y-0.5 text-xs">
                  {candidate.channels.map((channel) => (
                    <li key={channel}>
                      {describeRetrievalChannel(channel)}
                      {candidate.channel_scores[channel] !== undefined && (
                        <span className="ml-1 tabular-nums text-slate-700">
                          {formatScore(candidate.channel_scores[channel])}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </td>
              <td className={NUM_CELL}>{formatScore(candidate.total_score)}</td>
              <td className={CELL}>
                {candidate.rejection === null ? (
                  <>
                    <StatusBadge presentation={KEPT} />
                    {candidate.plan_id !== null && (
                      <Link
                        href={planHref(candidate.plan_id)}
                        className="mt-1 block text-xs text-blue-800 underline underline-offset-2"
                      >
                        View plan
                      </Link>
                    )}
                  </>
                ) : (
                  <>
                    <StatusBadge presentation={REJECTED} />
                    <span className="mt-1 block">{describeCandidateRejection(candidate.rejection)}</span>
                    <span className="block font-mono text-xs text-slate-600">{candidate.rejection}</span>
                  </>
                )}
              </td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
