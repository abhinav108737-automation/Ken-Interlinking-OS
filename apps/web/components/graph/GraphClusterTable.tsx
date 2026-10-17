import Link from "next/link";

import type { GraphClusterSummary } from "@/lib/api-client";
import { formatCount } from "@/lib/format";
import { clusterLabel, clusterPagesHref } from "@/lib/graph";
import { formatScore } from "@/lib/intelligence";

import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = [
  "Cluster",
  "Pages",
  "True orphans",
  "Nearly orphans",
  "Bridges",
  "Mean inbound links",
  "Most central page",
] as const;

/** Topical clusters. True orphans are their own column; entry pages are not counted as orphans here. */
export function GraphClusterTable({ clusters }: { readonly clusters: readonly GraphClusterSummary[] }) {
  return (
    <TableFrame caption="Topical clusters" count={clusters.length}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {clusters.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>
            No cluster. Graph metrics have not been computed, or no page has a topic.
          </EmptyRow>
        ) : (
          clusters.map((cluster) => {
            const href = clusterPagesHref(cluster.cluster_id);
            return (
              <tr key={cluster.cluster_id ?? "none"} className={ROW}>
                <th scope="row" className={`${CELL} font-medium`}>
                  {href === null ? (
                    clusterLabel(null)
                  ) : (
                    <Link href={href} className="text-blue-800 underline underline-offset-2">
                      {clusterLabel(cluster.cluster_id)}
                    </Link>
                  )}
                </th>
                <td className={NUM_CELL}>{formatCount(cluster.pages)}</td>
                <td className={NUM_CELL}>{formatCount(cluster.true_orphans)}</td>
                <td className={NUM_CELL}>{formatCount(cluster.nearly_orphans)}</td>
                <td className={NUM_CELL}>{formatCount(cluster.bridges)}</td>
                <td className={NUM_CELL}>{formatScore(cluster.mean_in_degree)}</td>
                <td className={`${CELL} max-w-xs break-all`}>
                  {cluster.top_page_id === null ? (
                    <span className="text-slate-600">—</span>
                  ) : (
                    <Link href={pageHref(cluster.top_page_id)} className="font-mono text-xs text-blue-800 underline">
                      {cluster.top_page_url ?? cluster.top_page_id}
                    </Link>
                  )}
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
