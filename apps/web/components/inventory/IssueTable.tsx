import type { IngestionIssueView } from "@/lib/api-client";
import { formatDateTime } from "@/lib/format";
import { describeIssueSeverity } from "@/lib/inventory";

import { StatusBadge } from "../StatusBadge";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "./Table";

const COLUMNS = ["Severity", "Code", "URL", "HTTP", "Message", "Occurred"];

/** Per-URL issues. URLs are shown as text only — never as links. */
export function IssueTable({
  issues,
  total,
  emptyMessage,
}: {
  readonly issues: readonly IngestionIssueView[];
  readonly total: number;
  readonly emptyMessage: string;
}) {
  return (
    <TableFrame caption="Issues" count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {issues.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>{emptyMessage}</EmptyRow>
        ) : (
          issues.map((issue) => (
            <tr key={issue.id} className={ROW}>
              <td className={CELL}>
                <StatusBadge presentation={describeIssueSeverity(issue.severity)} />
              </td>
              <td className={`${CELL} font-mono text-xs`}>{issue.code}</td>
              <td className={CELL}>
                {issue.url === null ? (
                  <span className="text-slate-600">—</span>
                ) : (
                  <span className="break-all font-mono text-xs">{issue.url}</span>
                )}
                {issue.related_url !== null && (
                  <span className="block break-all font-mono text-xs text-slate-700">
                    related: {issue.related_url}
                  </span>
                )}
              </td>
              <td className={NUM_CELL}>{issue.http_status ?? "—"}</td>
              <td className={CELL}>{issue.message}</td>
              <td className={`${CELL} whitespace-nowrap`}>
                <time dateTime={issue.occurred_at}>{formatDateTime(issue.occurred_at)}</time>
              </td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
