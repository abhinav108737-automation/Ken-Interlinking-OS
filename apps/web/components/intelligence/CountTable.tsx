import Link from "next/link";

import { formatCount } from "@/lib/format";

import { CELL, EmptyRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

export interface CountRow {
  /** Stable key (an id or enum value); shown in monospace beside the label when they differ. */
  readonly key: string;
  readonly label: string;
  readonly count: number;
  /** Internal href for the row (e.g. a taxonomy node's pages). */
  readonly href?: string;
}

/** A captioned two-column table of labelled counts. Text only. */
export function CountTable({
  caption,
  rows,
  keyHeader,
  valueHeader = "Count",
  emptyMessage,
}: {
  readonly caption: string;
  readonly rows: readonly CountRow[];
  readonly keyHeader: string;
  readonly valueHeader?: string;
  readonly emptyMessage: string;
}) {
  return (
    <TableFrame caption={caption} count={rows.length}>
      <thead className="bg-slate-100 text-slate-900">
        <tr>
          <th scope="col" className={`${CELL} font-semibold`}>
            {keyHeader}
          </th>
          <th scope="col" className={`${NUM_CELL} font-semibold`}>
            {valueHeader}
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <EmptyRow colSpan={2}>{emptyMessage}</EmptyRow>
        ) : (
          rows.map((row) => (
            <tr key={row.key} className={ROW}>
              <th scope="row" className={`${CELL} font-medium`}>
                {row.href === undefined ? (
                  row.label
                ) : (
                  <Link href={row.href} className="text-blue-800 underline underline-offset-2">
                    {row.label}
                  </Link>
                )}
                {row.label !== row.key && (
                  <span className="ml-2 font-mono text-xs font-normal text-slate-600">{row.key}</span>
                )}
              </th>
              <td className={NUM_CELL}>{formatCount(row.count)}</td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
