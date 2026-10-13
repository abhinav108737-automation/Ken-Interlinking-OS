import Link from "next/link";

import type { PageVersionSummary } from "@/lib/api-client";
import { formatCount, formatDateTime, shortHash } from "@/lib/format";
import { buildHref, describePublicationState, describeSourceType } from "@/lib/inventory";

import { StatusBadge } from "../StatusBadge";
import { pageHref } from "./PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "./Table";

const COLUMNS = ["Version", "Fetched", "Source", "HTTP", "Hash", "Publication", "Words", "Links (int / ext)"];

export function PageVersionTable({
  pageId,
  versions,
  total,
  shownVersion,
}: {
  readonly pageId: string;
  readonly versions: readonly PageVersionSummary[];
  readonly total: number;
  /** The version whose content is displayed on the page. */
  readonly shownVersion: number;
}) {
  return (
    <TableFrame caption="Version history" count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {versions.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>No versions are recorded for this page.</EmptyRow>
        ) : (
          versions.map((version) => {
            const shown = version.version === shownVersion;
            return (
              <tr key={version.version} className={ROW}>
                <th scope="row" className={`${CELL} font-medium`}>
                  {shown ? (
                    <span aria-current="true">
                      v{version.version} <span className="font-normal text-slate-700">(shown)</span>
                    </span>
                  ) : (
                    <Link
                      href={buildHref(pageHref(pageId), { version: version.version })}
                      className="text-blue-800 underline underline-offset-2"
                    >
                      v{version.version}
                      <span className="sr-only"> — inspect this version</span>
                    </Link>
                  )}
                </th>
                <td className={`${CELL} whitespace-nowrap`}>
                  <time dateTime={version.fetched_at}>{formatDateTime(version.fetched_at)}</time>
                </td>
                <td className={CELL}>{describeSourceType(version.source_type)}</td>
                <td className={NUM_CELL}>{version.http_status ?? "—"}</td>
                <td className={CELL}>
                  <code className="text-xs" title={version.content_hash}>
                    {shortHash(version.content_hash)}
                  </code>
                </td>
                <td className={CELL}>
                  <StatusBadge presentation={describePublicationState(version.publication_state)} />
                </td>
                <td className={NUM_CELL}>{formatCount(version.word_count)}</td>
                <td className={NUM_CELL}>
                  {formatCount(version.internal_link_count)} / {formatCount(version.external_link_count)}
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
