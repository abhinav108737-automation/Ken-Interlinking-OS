import Link from "next/link";

import type { CandidateView } from "@/lib/api-client";
import { formatScore } from "@/lib/intelligence";
import { describeCandidateRejection, describeRetrievalChannel, planHref } from "@/lib/recommendations";
import type { StatusPresentation } from "@/lib/status";

import { StatusBadge } from "../StatusBadge";
import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Target page", "Channels", "Graph position", "Total score", "Outcome"];

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

/**
 * Retrieved candidates, kept or rejected, with the first rejection rule
 * (ADR-0011 §3) and the per-channel attribution ADR-0018 added: each channel's
 * raw and normalised score and its rank within that channel. The per-signal
 * numbers are always visible, never collapsed into the total alone.
 */
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
                  {candidate.channels.map((channel) => {
                    const attribution = candidate.attribution.find((item) => item.channel === channel);
                    return (
                      <li key={channel}>
                        {describeRetrievalChannel(channel)}
                        {candidate.channel_scores[channel] !== undefined && (
                          <span className="ml-1 tabular-nums text-slate-700">
                            {formatScore(candidate.channel_scores[channel])}
                          </span>
                        )}
                        {attribution !== undefined && (
                          <span className="block text-slate-700">
                            raw <span className="tabular-nums">{formatScore(attribution.raw)}</span> · normalised{" "}
                            <span className="tabular-nums">{formatScore(attribution.normalised)}</span> · rank{" "}
                            <span className="tabular-nums">{attribution.rank_in_channel}</span>
                          </span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </td>
              <td className={CELL}>
                {candidate.graph_signals === null ? (
                  <span className="text-xs text-slate-600">Not computed</span>
                ) : (
                  <span className="block text-xs text-slate-700">
                    {candidate.graph_signals.is_orphan
                      ? "Orphan"
                      : candidate.graph_signals.is_nearly_orphan
                        ? "Nearly orphan"
                        : "Linked"}
                    {" · in "}
                    <span className="tabular-nums">{candidate.graph_signals.in_degree}</span>
                    {" / out "}
                    <span className="tabular-nums">{candidate.graph_signals.out_degree}</span>
                  </span>
                )}
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
