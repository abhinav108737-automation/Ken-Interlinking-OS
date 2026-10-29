import Link from "next/link";

import type { ProposalSummary } from "@/lib/api-client";
import { formatDateTime } from "@/lib/format";
import {
  describeProposalKind,
  describeProposalStatus,
  describeTargetKind,
  proposalHref,
} from "@/lib/learning";
import { describeActor } from "@/lib/recommendations";

import { StatusBadge } from "../StatusBadge";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Status", "Kind", "Title", "Target", "Proposed by", "Base version", "Updated"] as const;

/**
 * Policy and skill proposals. A proposal changes nothing on its own: an agent
 * may propose, but only a human promotes, and promotion is refused outright
 * while demonstration mode is on.
 *
 * `title` and `proposed_by` are model-influenced text and are rendered as text.
 */
export function ProposalTable({
  proposals,
  total,
  caption = "Learning proposals",
}: {
  readonly proposals: readonly ProposalSummary[];
  readonly total: number;
  readonly caption?: string;
}) {
  return (
    <TableFrame caption={caption} count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {proposals.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>
            No proposal yet — run <code>make reflect</code> to generate some from recorded experience.
          </EmptyRow>
        ) : (
          proposals.map((proposal) => {
            const actor = describeActor(proposal.proposed_by);
            return (
              <tr key={proposal.id} className={ROW}>
                <th scope="row" className={CELL}>
                  <StatusBadge presentation={describeProposalStatus(proposal.status)} />
                </th>
                <td className={CELL}>{describeProposalKind(proposal.kind)}</td>
                <td className={`${CELL} max-w-sm break-words`}>
                  <Link href={proposalHref(proposal.id)} className="text-blue-800 underline underline-offset-2">
                    {proposal.title}
                  </Link>
                  {proposal.supersedes !== null && (
                    <span className="block text-xs text-slate-700">Supersedes an earlier proposal</span>
                  )}
                </td>
                <td className={CELL}>
                  {proposal.target_kind === null ? (
                    <span className="text-slate-600">—</span>
                  ) : (
                    describeTargetKind(proposal.target_kind)
                  )}
                </td>
                <td className={CELL}>
                  <span className="whitespace-nowrap">{actor.label}</span>
                  <span className="block break-all font-mono text-xs text-slate-700">{actor.id}</span>
                  {proposal.fallback_used && (
                    <span className="block text-xs text-amber-900">Fallback was used</span>
                  )}
                </td>
                <td className={CELL}>
                  <code className="text-xs">{proposal.base_version}</code>
                </td>
                <td className={CELL}>
                  <time dateTime={proposal.updated_at} className="whitespace-nowrap text-xs">
                    {formatDateTime(proposal.updated_at)}
                  </time>
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
