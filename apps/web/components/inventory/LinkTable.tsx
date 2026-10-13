import Link from "next/link";

import type { ExistingLinkView } from "@/lib/api-client";
import { describeLinkResolution, describeLinkType } from "@/lib/inventory";

import { StatusBadge } from "../StatusBadge";
import { ExternalUrl } from "./ExternalUrl";
import { pageHref } from "./PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "./Table";

export interface LinkTableProps {
  readonly links: readonly ExistingLinkView[];
  readonly caption: string;
  readonly total: number;
  /** Show the page each link is on (for inbound links). */
  readonly showSource?: boolean;
  readonly emptyMessage: string;
}

/**
 * Body links. The raw `href` is always plain text (it may be `javascript:` or
 * relative); only the resolved http(s) URL can be a link, via ExternalUrl.
 */
export function LinkTable({ links, caption, total, showSource = false, emptyMessage }: LinkTableProps) {
  const columns = [
    ...(showSource ? ["Linking page"] : []),
    "Anchor text",
    "Href",
    "Resolved URL",
    "Type",
    "Resolution",
    "Rel",
    "Block",
  ];
  return (
    <TableFrame caption={caption} count={total}>
      <HeaderRow columns={columns} />
      <tbody>
        {links.length === 0 ? (
          <EmptyRow colSpan={columns.length}>{emptyMessage}</EmptyRow>
        ) : (
          links.map((link) => (
            <tr key={`${link.page_id}:${link.page_version}:${link.ordinal}`} className={ROW}>
              {showSource && (
                <td className={CELL}>
                  <Link
                    href={pageHref(link.page_id)}
                    className="break-all font-mono text-xs text-blue-800 underline underline-offset-2"
                  >
                    {link.source_url}
                  </Link>
                  <span className="block text-xs text-slate-600">version {link.page_version}</span>
                </td>
              )}
              <td className={CELL}>
                {link.anchor_text === "" ? (
                  <span className="text-slate-600">(no anchor text)</span>
                ) : (
                  link.anchor_text
                )}
              </td>
              <td className={CELL}>
                <code className="break-all text-xs">{link.href}</code>
              </td>
              <td className={CELL}>
                {link.resolved_url === null ? (
                  <span className="text-slate-600">—</span>
                ) : (
                  <ExternalUrl url={link.resolved_url} />
                )}
              </td>
              <td className={CELL}>{describeLinkType(link.link_type)}</td>
              <td className={CELL}>
                <StatusBadge presentation={describeLinkResolution(link.resolution)} />
                {link.target_page_id !== null && (
                  <Link
                    href={pageHref(link.target_page_id)}
                    className="mt-1 block text-xs text-blue-800 underline underline-offset-2"
                  >
                    Target page
                  </Link>
                )}
              </td>
              <td className={CELL}>{link.rel.length === 0 ? "—" : link.rel.join(" ")}</td>
              <td className={CELL}>
                <span className="font-mono text-xs">{link.block_id}</span>
                <span className="block text-xs text-slate-600">{link.block_kind}</span>
              </td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
