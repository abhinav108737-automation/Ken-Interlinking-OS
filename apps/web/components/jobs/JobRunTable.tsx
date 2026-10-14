import Link from "next/link";

import type { JobRunSummary } from "@/lib/api-client";
import { formatCount, formatDateTime } from "@/lib/format";
import { describeJobKind, humanizeKey, jobRunHref } from "@/lib/intelligence";
import { describeRunStatus } from "@/lib/inventory";

import { StatusBadge } from "../StatusBadge";
import { runDuration } from "../inventory/RunTable";
import { CELL, HeaderRow, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Kind", "Status", "Triggered by", "Started", "Completed", "Duration", "Counters"];

/** Counter names are kind-specific (documented per kind), so they are listed rather than given columns. */
export function JobCounters({ counters }: { readonly counters: Readonly<Record<string, number>> }) {
  const rows = Object.entries(counters)
    .map(([key, count]) => ({ key, count }))
    .sort((a, b) => a.key.localeCompare(b.key, "en"));
  if (rows.length === 0) {
    return <span className="text-slate-600">None recorded</span>;
  }
  return (
    <dl className="grid grid-cols-[max-content_max-content] gap-x-3 text-xs">
      {rows.map(({ key, count }) => (
        <div key={key} className="contents">
          <dt className="text-slate-700">{humanizeKey(key)}</dt>
          <dd className="text-right tabular-nums">{formatCount(count)}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Batch job runs. Expects a non-empty list; the screen renders the empty state. */
export function JobRunTable({ runs, total }: { readonly runs: readonly JobRunSummary[]; readonly total: number }) {
  return (
    <TableFrame caption="Job runs" count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {runs.map((run) => (
          <tr key={run.id} className={ROW}>
            <th scope="row" className={`${CELL} font-medium`}>
              <Link href={jobRunHref(run.id)} className="text-blue-800 underline underline-offset-2">
                {describeJobKind(run.kind)}
                <span className="sr-only"> run started {formatDateTime(run.started_at)}</span>
              </Link>
            </th>
            <td className={CELL}>
              <StatusBadge presentation={describeRunStatus(run.status)} />
            </td>
            <td className={`${CELL} break-words`}>{run.triggered_by}</td>
            <td className={`${CELL} whitespace-nowrap`}>
              <time dateTime={run.started_at}>{formatDateTime(run.started_at)}</time>
            </td>
            <td className={`${CELL} whitespace-nowrap`}>{formatDateTime(run.completed_at)}</td>
            <td className={`${CELL} whitespace-nowrap`}>{runDuration(run)}</td>
            <td className={CELL}>
              <JobCounters counters={run.counters} />
            </td>
          </tr>
        ))}
      </tbody>
    </TableFrame>
  );
}
