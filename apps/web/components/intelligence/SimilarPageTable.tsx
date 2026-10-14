import Link from "next/link";

import type { SimilarPage } from "@/lib/api-client";
import { type TaxonomyLabels, formatScore, nodeLabel, taxonomyNodeHref } from "@/lib/intelligence";
import { pageDisplayTitle } from "@/lib/inventory";

import { ExternalUrl } from "../inventory/ExternalUrl";
import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

/**
 * Pages with a score: similar pages, semantic-search hits and taxonomy-node
 * pages all share the contract's `SimilarPage` shape. Titles and URLs come
 * from crawled content and are rendered as text (URLs via ExternalUrl).
 */
export function SimilarPageTable({
  pages,
  caption,
  total,
  labels,
  scoreHeader = "Score",
  emptyMessage,
}: {
  readonly pages: readonly SimilarPage[];
  readonly caption: string;
  readonly total?: number;
  readonly labels: TaxonomyLabels;
  readonly scoreHeader?: string;
  readonly emptyMessage: string;
}) {
  const columns = [scoreHeader, "Page", "URL", "Page type", "Market"];
  return (
    <TableFrame caption={caption} count={total ?? pages.length}>
      <HeaderRow columns={columns} />
      <tbody>
        {pages.length === 0 ? (
          <EmptyRow colSpan={columns.length}>{emptyMessage}</EmptyRow>
        ) : (
          pages.map((page) => (
            <tr key={page.page_id} className={ROW}>
              <td className={NUM_CELL}>{formatScore(page.score)}</td>
              <th scope="row" className={`${CELL} font-medium`}>
                <Link href={pageHref(page.page_id)} className="break-words text-blue-800 underline underline-offset-2">
                  {pageDisplayTitle(page)}
                </Link>
              </th>
              <td className={CELL}>
                <ExternalUrl url={page.canonical_url} />
              </td>
              <td className={CELL}>{page.page_type ?? <span className="text-slate-600">—</span>}</td>
              <td className={CELL}>
                {page.market_id === null ? (
                  <span className="text-slate-600">—</span>
                ) : (
                  <Link href={taxonomyNodeHref(page.market_id)} className="text-blue-800 underline underline-offset-2">
                    {nodeLabel(labels, page.market_id)}
                  </Link>
                )}
              </td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
