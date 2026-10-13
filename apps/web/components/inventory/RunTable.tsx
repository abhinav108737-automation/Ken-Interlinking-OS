import Link from "next/link";

import type { IngestionRunSummary, RunCounters } from "@/lib/api-client";
import { NONE, durationBetween, formatCount, formatDateTime, formatDuration } from "@/lib/format";
import { describeRunStatus, describeSourceType } from "@/lib/inventory";

import { StatusBadge } from "../StatusBadge";
import { CELL, NUM_CELL, ROW, TableFrame } from "./Table";

/** Counter columns in display order: [field, short header, full description]. */
export const COUNTER_FIELDS: readonly (readonly [keyof RunCounters, string, string])[] = [
  ["pages_discovered", "Disc.", "Discovered"],
  ["pages_fetched", "Fetched", "Fetched"],
  ["pages_created", "Created", "Created"],
  ["pages_changed", "Changed", "Changed"],
  ["pages_unchanged", "Unch.", "Unchanged"],
  ["pages_duplicate", "Dup.", "Duplicate"],
  ["pages_failed", "Failed", "Failed"],
  ["pages_skipped", "Skipped", "Skipped"],
  ["pages_gone", "Gone", "Gone"],
  ["error_count", "Errors", "Errors"],
  ["warning_count", "Warn.", "Warnings"],
];

export function runHref(runId: string): string {
  return `/inventory/runs/${encodeURIComponent(runId)}`;
}

/** "12.3 s", or why there is no duration. */
export function runDuration(run: Pick<IngestionRunSummary, "started_at" | "completed_at" | "status">): string {
  const ms = durationBetween(run.started_at, run.completed_at);
  if (ms !== null) return formatDuration(ms);
  return run.completed_at === null && run.status === "running" ? "In progress" : NONE;
}

export function RunTable({ runs, total }: { readonly runs: readonly IngestionRunSummary[]; readonly total: number }) {
  return (
    <TableFrame caption="Ingestion runs" count={total}>
      <thead className="bg-slate-100 text-slate-900">
        <tr>
          {["Source", "Status", "Started", "Completed", "Duration"].map((column) => (
            <th key={column} scope="col" className="whitespace-nowrap px-3 py-2 font-semibold">
              {column}
            </th>
          ))}
          {COUNTER_FIELDS.map(([field, short, full]) => (
            <th key={field} scope="col" className="whitespace-nowrap px-3 py-2 text-right font-semibold">
              {short === full ? full : <abbr title={full}>{short}</abbr>}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => (
          <tr key={run.id} className={ROW}>
            <th scope="row" className={`${CELL} font-medium`}>
              <Link href={runHref(run.id)} className="text-blue-800 underline underline-offset-2">
                {run.source_name}
              </Link>
              <span className="block text-xs font-normal text-slate-700">{describeSourceType(run.source_type)}</span>
            </th>
            <td className={CELL}>
              <StatusBadge presentation={describeRunStatus(run.status)} />
            </td>
            <td className={`${CELL} whitespace-nowrap`}>
              <time dateTime={run.started_at}>{formatDateTime(run.started_at)}</time>
            </td>
            <td className={`${CELL} whitespace-nowrap`}>{formatDateTime(run.completed_at)}</td>
            <td className={`${CELL} whitespace-nowrap`}>{runDuration(run)}</td>
            {COUNTER_FIELDS.map(([field]) => (
              <td key={field} className={NUM_CELL}>
                {formatCount(run.counters[field])}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </TableFrame>
  );
}

/** One run's counters as a description list. */
export function RunCountersList({ counters }: { readonly counters: RunCounters }) {
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
      {COUNTER_FIELDS.map(([field, , full]) => (
        <div key={field} className="flex justify-between gap-2 border-b border-slate-200 py-1">
          <dt className="text-slate-700">{full}</dt>
          <dd className="font-medium tabular-nums">{formatCount(counters[field])}</dd>
        </div>
      ))}
    </dl>
  );
}
