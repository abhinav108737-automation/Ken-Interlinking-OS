import Link from "next/link";

import type { OpportunityView } from "@/lib/api-client";
import { formatDateTime, toDisplayEntries } from "@/lib/format";
import { formatScore, jobRunHref } from "@/lib/intelligence";
import { pageDisplayTitle } from "@/lib/inventory";

import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Page", "Score", "Evidence", "Computed"];

/** Structured evidence as text key/value pairs (credential-looking keys withheld). */
function Evidence({ evidence }: { readonly evidence: Readonly<Record<string, unknown>> }) {
  const entries = toDisplayEntries(evidence);
  if (entries.length === 0) return <span className="text-slate-600">—</span>;
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-3 text-xs">
      {entries.map((entry) => (
        <div key={entry.key} className="contents">
          <dt className="font-mono text-slate-700">{entry.key}</dt>
          <dd className="break-all font-mono">{entry.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** One kind of opportunity, strongest first (API order). */
export function OpportunityTable({
  caption,
  opportunities,
  total,
  emptyMessage,
}: {
  readonly caption: string;
  readonly opportunities: readonly OpportunityView[];
  readonly total: number;
  readonly emptyMessage: string;
}) {
  return (
    <TableFrame caption={caption} count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {opportunities.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>{emptyMessage}</EmptyRow>
        ) : (
          opportunities.map((opportunity) => (
            <tr key={`${opportunity.kind}:${opportunity.page_id}`} className={ROW}>
              <th scope="row" className={`${CELL} font-medium`}>
                <Link href={pageHref(opportunity.page_id)} className="break-words text-blue-800 underline underline-offset-2">
                  {pageDisplayTitle(opportunity)}
                </Link>
                <span className="block break-all font-mono text-xs font-normal text-slate-600">
                  {opportunity.canonical_url}
                </span>
              </th>
              <td className={NUM_CELL}>{formatScore(opportunity.score)}</td>
              <td className={CELL}>
                <Evidence evidence={opportunity.evidence} />
              </td>
              <td className={`${CELL} whitespace-nowrap`}>
                <time dateTime={opportunity.computed_at}>{formatDateTime(opportunity.computed_at)}</time>
                <Link
                  href={jobRunHref(opportunity.run_id)}
                  className="block text-xs text-blue-800 underline underline-offset-2"
                >
                  Run{" "}
                  <span className="sr-only">{opportunity.run_id}</span>
                </Link>
              </td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
