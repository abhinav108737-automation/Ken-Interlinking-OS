import Link from "next/link";

import type { GraphPageRow } from "@/lib/api-client";
import { formatCount } from "@/lib/format";
import { BRIDGE_PRESENTATION, describeGraphPosition, graphPositionOf } from "@/lib/graph";
import { formatScore } from "@/lib/intelligence";
import { pageDisplayTitle } from "@/lib/inventory";

import { StatusBadge } from "../StatusBadge";
import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Page", "Position", "In", "Out", "PageRank", "Authority", "Hub", "Cluster", "Component", "Depth"] as const;

/** Per-page graph position. The entry page is labelled as one, never as an orphan to fix. */
export function GraphPageTable({
  rows,
  total,
  caption = "Pages in the link graph",
}: {
  readonly rows: readonly GraphPageRow[];
  readonly total: number;
  readonly caption?: string;
}) {
  return (
    <TableFrame caption={caption} count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {rows.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>No page matches these filters.</EmptyRow>
        ) : (
          rows.map((row) => {
            const s = row.signals;
            return (
              <tr key={s.page_id} className={ROW}>
                <th scope="row" className={`${CELL} max-w-sm break-words font-medium`}>
                  <Link href={pageHref(s.page_id)} className="text-blue-800 underline underline-offset-2">
                    {pageDisplayTitle({ title: row.title, canonical_url: row.canonical_url })}
                  </Link>
                  <span className="block break-all font-mono text-xs font-normal text-slate-700">
                    {row.canonical_url}
                  </span>
                </th>
                <td className={CELL}>
                  <StatusBadge presentation={describeGraphPosition(graphPositionOf(row))} />
                  {s.is_bridge && (
                    <span className="mt-1 block">
                      <StatusBadge presentation={BRIDGE_PRESENTATION} />
                    </span>
                  )}
                </td>
                <td className={NUM_CELL}>{formatCount(s.in_degree)}</td>
                <td className={NUM_CELL}>{formatCount(s.out_degree)}</td>
                <td className={NUM_CELL}>{formatScore(s.internal_pagerank)}</td>
                <td className={NUM_CELL}>{formatScore(s.authority_score)}</td>
                <td className={NUM_CELL}>{formatScore(s.hub_score)}</td>
                <td className={NUM_CELL}>{s.cluster_id === null ? "—" : `#${s.cluster_id}`}</td>
                <td className={NUM_CELL}>{s.component_id === null ? "—" : `#${s.component_id}`}</td>
                <td className={NUM_CELL}>{s.crawl_depth === null ? "Unreachable" : s.crawl_depth}</td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
