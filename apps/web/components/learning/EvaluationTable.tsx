import type { EvaluationSummary } from "@/lib/api-client";
import { formatDateTime } from "@/lib/format";
import { describeEvalSuite, describeEvaluationMode, describeGateResult } from "@/lib/learning";

import { StatusBadge } from "../StatusBadge";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Gate result", "Mode", "Suites", "Proposal", "Runner", "Started", "Finished"] as const;

/**
 * Evaluation runs and their gate outcomes — the learning OUTCOMES surface.
 *
 * `inconclusive` is shown as its own result and never as a pass: below the
 * sample floor a gate cannot reach a verdict, and treating that as success is
 * the exact failure the sample floor exists to prevent.
 */
export function EvaluationTable({
  evaluations,
  total,
  caption = "Evaluation runs",
}: {
  readonly evaluations: readonly EvaluationSummary[];
  readonly total: number;
  readonly caption?: string;
}) {
  return (
    <TableFrame caption={caption} count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {evaluations.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>
            No evaluation run yet — run <code>make evaluate</code>.
          </EmptyRow>
        ) : (
          evaluations.map((run) => (
            <tr key={run.id} className={ROW}>
              <th scope="row" className={CELL}>
                <StatusBadge presentation={describeGateResult(run.gate_result)} />
              </th>
              <td className={CELL}>{describeEvaluationMode(run.mode)}</td>
              <td className={`${CELL} max-w-sm`}>
                {run.suites.length === 0 ? (
                  <span className="text-slate-600">None</span>
                ) : (
                  <ul className="list-none text-xs">
                    {run.suites.map((suite) => (
                      <li key={suite}>{describeEvalSuite(suite)}</li>
                    ))}
                  </ul>
                )}
              </td>
              <td className={CELL}>
                {run.proposal_id === null ? (
                  <span className="text-slate-600">Baseline (no proposal)</span>
                ) : (
                  <span className="break-all font-mono text-xs">{run.proposal_id}</span>
                )}
              </td>
              <td className={CELL}>
                <code className="text-xs">{run.runner_version}</code>
                <span className="block text-xs text-slate-700">{run.model_provider}</span>
                {run.git_sha !== null && (
                  <span className="block font-mono text-xs text-slate-700">{run.git_sha.slice(0, 12)}</span>
                )}
              </td>
              <td className={CELL}>
                <time dateTime={run.started_at} className="whitespace-nowrap text-xs">
                  {formatDateTime(run.started_at)}
                </time>
              </td>
              <td className={CELL}>
                {run.finished_at === null ? (
                  <span className="text-slate-600">Running</span>
                ) : (
                  <time dateTime={run.finished_at} className="whitespace-nowrap text-xs">
                    {formatDateTime(run.finished_at)}
                  </time>
                )}
              </td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
