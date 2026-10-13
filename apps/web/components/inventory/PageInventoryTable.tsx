import Link from "next/link";

import type { PageSummary } from "@/lib/api-client";
import { formatCount, formatDateTime } from "@/lib/format";
import { GONE_PRESENTATION, describePublicationState, pageDisplayTitle } from "@/lib/inventory";

import { StatusBadge } from "../StatusBadge";
import { CELL, HeaderRow, NUM_CELL, ROW, TableFrame } from "./Table";

const COLUMNS = [
  "Title",
  "Canonical URL",
  "Publication",
  "Language",
  "Version",
  "Words",
  "Internal links",
  "External links",
  "Last changed",
] as const;

export function pageHref(pageId: string): string {
  return `/inventory/pages/${encodeURIComponent(pageId)}`;
}

/** The page inventory. Expects a non-empty list; the screen renders the empty state. */
export function PageInventoryTable({
  pages,
  total,
}: {
  readonly pages: readonly PageSummary[];
  readonly total: number;
}) {
  return (
    <TableFrame caption="Pages" count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {pages.map((page) => (
          <tr key={page.id} className={ROW}>
            <th scope="row" className={`${CELL} font-medium`}>
              <Link
                href={pageHref(page.id)}
                className="break-words text-blue-800 underline underline-offset-2"
              >
                {pageDisplayTitle(page)}
              </Link>
              {page.gone_at !== null && (
                <span className="ml-2 inline-block">
                  <StatusBadge presentation={GONE_PRESENTATION} />
                </span>
              )}
            </th>
            <td className={CELL}>
              <span className="break-all font-mono text-xs">{page.canonical_url}</span>
            </td>
            <td className={CELL}>
              <StatusBadge presentation={describePublicationState(page.publication_state)} />
            </td>
            <td className={CELL}>{page.language ?? "—"}</td>
            <td className={NUM_CELL}>{page.current_version}</td>
            <td className={NUM_CELL}>{formatCount(page.word_count)}</td>
            <td className={NUM_CELL}>{formatCount(page.internal_link_count)}</td>
            <td className={NUM_CELL}>{formatCount(page.external_link_count)}</td>
            <td className={`${CELL} whitespace-nowrap`}>
              <time dateTime={page.last_changed_at}>{formatDateTime(page.last_changed_at)}</time>
            </td>
          </tr>
        ))}
      </tbody>
    </TableFrame>
  );
}
