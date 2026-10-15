import Link from "next/link";

import type { VerificationRun } from "@/lib/api-client";
import { describeVerificationCheck, describeVerificationOutcome, describeVerificationRun } from "@/lib/deployments";
import { formatDateTime } from "@/lib/format";
import { planHref } from "@/lib/recommendations";

import { StatusBadge } from "../StatusBadge";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "../inventory/Table";

/** Every verification run of a deployment, with each check's outcome (incl. inconclusive). */
export function VerificationRuns({
  runs,
  planLabels,
}: {
  readonly runs: readonly VerificationRun[];
  readonly planLabels: Readonly<Record<string, string>>;
}) {
  if (runs.length === 0) {
    return (
      <p role="status" className="text-slate-700">
        No verification has run yet.
      </p>
    );
  }
  return (
    <ol className="space-y-4">
      {runs.map((run) => {
        const verdict = describeVerificationRun(run);
        return (
          <li key={run.id} className="space-y-2">
            <p className="flex flex-wrap items-center gap-3 text-sm">
              <span className="font-medium">
                Run {run.id} at <time dateTime={run.run_at}>{formatDateTime(run.run_at)}</time>
              </span>
              <StatusBadge presentation={verdict} />
              <span className="text-slate-700">
                {run.http_checked ? "Checked over HTTP and in the CMS" : "Checked in the CMS only (no HTTP verification)"}
              </span>
            </p>
            <TableFrame caption={`Checks of verification run ${run.id}`} count={run.checks.length}>
              <HeaderRow columns={["Link", "Check", "Outcome", "Detail"]} />
              <tbody>
                {run.checks.length === 0 ? (
                  <EmptyRow colSpan={4}>No checks were recorded.</EmptyRow>
                ) : (
                  run.checks.map((check, index) => (
                    <tr key={`${check.plan_id}-${check.check}-${index}`} className={ROW}>
                      <td className={CELL}>
                        <Link href={planHref(check.plan_id)} className="break-all text-blue-800 underline underline-offset-2">
                          {planLabels[check.plan_id.toLowerCase()] ?? check.plan_id}
                        </Link>
                      </td>
                      <td className={CELL}>{describeVerificationCheck(check.check)}</td>
                      <td className={CELL}>
                        <StatusBadge presentation={describeVerificationOutcome(check.outcome)} />
                      </td>
                      <td className={CELL}>
                        {check.detail === null || check.detail === undefined || check.detail === "" ? (
                          <span className="text-slate-600">—</span>
                        ) : (
                          <code className="break-all text-xs">{check.detail}</code>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </TableFrame>
          </li>
        );
      })}
    </ol>
  );
}
