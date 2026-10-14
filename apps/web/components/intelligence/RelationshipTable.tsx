import Link from "next/link";

import type { PageRelationshipView } from "@/lib/api-client";
import { formatDateTime } from "@/lib/format";
import { describeRelationshipType, formatScore } from "@/lib/intelligence";
import { pageDisplayTitle } from "@/lib/inventory";
import { summarizeDetail } from "@/lib/status";

import { ExternalUrl } from "../inventory/ExternalUrl";
import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Type", "Weight", "Target page", "Target URL", "Versions", "Evidence", "Computed"];

/** Typed relationships from one page, strongest first (API order). Evidence is shown as compact text. */
export function RelationshipTable({
  relationships,
  total,
  emptyMessage,
}: {
  readonly relationships: readonly PageRelationshipView[];
  readonly total: number;
  readonly emptyMessage: string;
}) {
  return (
    <TableFrame caption="Related pages" count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {relationships.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>{emptyMessage}</EmptyRow>
        ) : (
          relationships.map((edge) => {
            const evidence = summarizeDetail(edge.evidence);
            return (
              <tr key={`${edge.relationship_type}:${edge.target_page_id}`} className={ROW}>
                <td className={CELL}>
                  {describeRelationshipType(edge.relationship_type)}
                  <span className="block font-mono text-xs text-slate-600">{edge.relationship_type}</span>
                </td>
                <td className={NUM_CELL}>{formatScore(edge.weight)}</td>
                <th scope="row" className={`${CELL} font-medium`}>
                  <Link
                    href={pageHref(edge.target_page_id)}
                    className="break-words text-blue-800 underline underline-offset-2"
                  >
                    {pageDisplayTitle({ title: edge.target_title, canonical_url: edge.target_canonical_url })}
                  </Link>
                </th>
                <td className={CELL}>
                  <ExternalUrl url={edge.target_canonical_url} />
                </td>
                <td className={`${CELL} whitespace-nowrap text-xs`}>
                  v{edge.source_page_version} → v{edge.target_page_version}
                </td>
                <td className={CELL}>
                  {evidence === "" ? (
                    <span className="text-slate-600">—</span>
                  ) : (
                    <span className="break-words font-mono text-xs">{evidence}</span>
                  )}
                </td>
                <td className={`${CELL} whitespace-nowrap`}>
                  <time dateTime={edge.computed_at}>{formatDateTime(edge.computed_at)}</time>
                  <span className="block font-mono text-xs text-slate-600">{edge.analysis_version}</span>
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
