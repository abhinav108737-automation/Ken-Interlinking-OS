import { SyntheticBadge } from "@/components/analytics/SyntheticNotice";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "@/components/inventory/Table";
import type { SourceFreshness } from "@/lib/api-client";
import { describeDataset, describeImportVia, describeSource } from "@/lib/analytics";
import { NONE, formatDateTime } from "@/lib/format";

/**
 * The newest stored day per (source, dataset), so an operator can see at a
 * glance how stale each source is. A source with no row at all is named with an
 * explicit "never imported", never silently omitted.
 */
export function SourceFreshnessTable({ sources }: { readonly sources: readonly SourceFreshness[] }) {
  return (
    <TableFrame caption="Source freshness" count={sources.length}>
      <HeaderRow columns={["Source", "Dataset", "Newest stored day", "Imported", "Via"]} />
      <tbody>
        {sources.length === 0 ? (
          <EmptyRow colSpan={5}>No source has been imported yet.</EmptyRow>
        ) : (
          sources.map((row) => (
            <tr key={`${row.source}:${row.dataset}`} className={ROW}>
              <th scope="row" className={`${CELL} whitespace-nowrap font-medium`}>
                {describeSource(row.source)} <SyntheticBadge synthetic={row.synthetic} />
              </th>
              <td className={CELL}>{describeDataset(row.dataset)}</td>
              <td className={CELL}>
                {row.latest_date === null ? (
                  <span className="text-slate-700">Never imported</span>
                ) : (
                  <time dateTime={row.latest_date}>{row.latest_date}</time>
                )}
              </td>
              <td className={CELL}>
                {row.last_imported_at === null ? (
                  <span className="text-slate-700">{NONE}</span>
                ) : (
                  <time dateTime={row.last_imported_at}>{formatDateTime(row.last_imported_at)}</time>
                )}
              </td>
              <td className={CELL}>
                {row.last_via === null ? <span className="text-slate-700">{NONE}</span> : describeImportVia(row.last_via)}
              </td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
