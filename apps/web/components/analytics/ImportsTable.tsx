import Link from "next/link";

import { SyntheticBadge } from "@/components/analytics/SyntheticNotice";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "@/components/inventory/Table";
import type { AnalyticsImportView } from "@/lib/api-client";
import { describeDataset, describeImportVia, describeSource } from "@/lib/analytics";
import { formatCount, formatDateTime, shortHash } from "@/lib/format";
import { jobRunHref } from "@/lib/intelligence";
import { describeActor } from "@/lib/recommendations";

/**
 * The import ledger, newest first. Each row was written once, on success; a
 * failed run exists only as a job run (ADR-0015 section 12.6.6), which is why
 * there is no status column here.
 */
export function ImportsTable({
  imports,
  total,
  caption = "Recent imports",
}: {
  readonly imports: readonly AnalyticsImportView[];
  readonly total?: number;
  readonly caption?: string;
}) {
  return (
    <TableFrame caption={caption} count={total ?? imports.length}>
      <HeaderRow columns={["Imported", "Source", "Dataset", "Days covered", "Rows", "Via", "File", "By"]} />
      <tbody>
        {imports.length === 0 ? (
          <EmptyRow colSpan={8}>No import has succeeded yet.</EmptyRow>
        ) : (
          imports.map((row) => {
            const actor = describeActor(row.triggered_by);
            return (
              <tr key={row.id} className={ROW}>
                <th scope="row" className={`${CELL} whitespace-nowrap font-medium`}>
                  <Link href={jobRunHref(row.job_run_id)} className="text-blue-800 underline underline-offset-2">
                    <time dateTime={row.created_at}>{formatDateTime(row.created_at)}</time>
                    <span className="sr-only"> — view the job run</span>
                  </Link>
                </th>
                <td className={CELL}>
                  {describeSource(row.source)} <SyntheticBadge synthetic={row.synthetic} />
                  {row.sampled && <span className="ml-1 text-xs text-amber-900">(sampled)</span>}
                </td>
                <td className={CELL}>{describeDataset(row.dataset)}</td>
                <td className={CELL}>
                  <time dateTime={row.date_start}>{row.date_start}</time> to{" "}
                  <time dateTime={row.date_end}>{row.date_end}</time>
                  <span className="block text-xs text-slate-700">Days are {row.source_timezone} days</span>
                </td>
                <td className={NUM_CELL}>{formatCount(row.rows_written)}</td>
                <td className={CELL}>{describeImportVia(row.via)}</td>
                <td className={CELL}>
                  {row.file_name === null ? (
                    <span className="text-slate-700">None (no file)</span>
                  ) : (
                    <>
                      <span className="break-all font-mono text-xs">{row.file_name}</span>
                      {row.file_sha256 !== null && (
                        <code className="block text-xs text-slate-700" title={row.file_sha256}>
                          {shortHash(row.file_sha256)}
                        </code>
                      )}
                    </>
                  )}
                </td>
                <td className={CELL}>
                  {actor.label}: {actor.id}
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
