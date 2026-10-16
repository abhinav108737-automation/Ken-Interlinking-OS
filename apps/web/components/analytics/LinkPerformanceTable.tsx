import Link from "next/link";

import { SyntheticBadge } from "@/components/analytics/SyntheticNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { ExternalUrl } from "@/components/inventory/ExternalUrl";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "@/components/inventory/Table";
import type { LinkPerformanceView } from "@/lib/api-client";
import {
  describeLiveWindow,
  describeSource,
  formatCtr,
  hasNoEvents,
  noEventsNote,
  sourcesOf,
  totalsFor,
} from "@/lib/analytics";
import { formatCount } from "@/lib/format";
import { describeLinkPlanStatus, planHref } from "@/lib/recommendations";

export interface LinkPerformanceTableProps {
  readonly links: readonly LinkPerformanceView[];
  readonly caption: string;
  readonly total?: number;
  readonly emptyMessage: string;
}

const LINK = "text-blue-800 underline underline-offset-2";

/** What a link's live window says about its measurement, in plain words. */
function LiveWindowCell({ link }: { readonly link: LinkPerformanceView }) {
  const window = { plan_id: link.plan_id, deployed_at: link.deployed_at, rolled_back_at: link.rolled_back_at };
  const presentation = describeLiveWindow(window);
  if (presentation.neverLive) {
    return <span className="text-slate-700">{presentation.description}</span>;
  }
  if (presentation.closed) {
    return (
      <span className="text-slate-700">
        Window closed: rolled back{" "}
        <time dateTime={link.rolled_back_at ?? undefined}>{(link.rolled_back_at ?? "").slice(0, 10)}</time>. No data is
        collected for it after that.
      </span>
    );
  }
  if (hasNoEvents(link.totals)) {
    return <span className="text-slate-700">{noEventsNote(window)}</span>;
  }
  return (
    <span className="text-slate-700">
      Live since <time dateTime={link.deployed_at ?? undefined}>{(link.deployed_at ?? "").slice(0, 10)}</time>
    </span>
  );
}

/**
 * Per-link totals over the requested range. Used for the top links on the
 * dashboard and for the links of one deployment. Impressions and clicks are the
 * sum over the sources that hold rows for that link; the "Sources" column names
 * them, because each source counts independently.
 */
export function LinkPerformanceTable({ links, caption, total, emptyMessage }: LinkPerformanceTableProps) {
  return (
    <TableFrame caption={caption} count={total ?? links.length}>
      <HeaderRow columns={["Anchor", "Status", "Target", "Sources", "Impressions", "Clicks", "CTR", "Live window"]} />
      <tbody>
        {links.length === 0 ? (
          <EmptyRow colSpan={8}>{emptyMessage}</EmptyRow>
        ) : (
          links.map((link) => {
            const totals = totalsFor(link.totals);
            const sources = sourcesOf(link.totals);
            return (
              <tr key={link.plan_id} className={ROW}>
                <th scope="row" className={`${CELL} font-medium`}>
                  <Link href={planHref(link.plan_id)} className={`${LINK} break-words`}>
                    {link.anchor_text}
                    <span className="sr-only"> — view recommendation</span>
                  </Link>{" "}
                  <SyntheticBadge synthetic={link.synthetic} />
                </th>
                <td className={CELL}>
                  <StatusBadge presentation={describeLinkPlanStatus(link.plan_status)} />
                </td>
                <td className={CELL}>
                  <ExternalUrl url={link.target_url} />
                </td>
                <td className={CELL}>
                  {sources.length === 0 ? (
                    <span className="text-slate-700">None</span>
                  ) : (
                    sources.map(describeSource).join(", ")
                  )}
                </td>
                <td className={NUM_CELL}>{formatCount(totals.impressions)}</td>
                <td className={NUM_CELL}>{formatCount(totals.clicks)}</td>
                <td className={NUM_CELL}>{formatCtr(totals.ctr)}</td>
                <td className={CELL}>
                  <LiveWindowCell link={link} />
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
