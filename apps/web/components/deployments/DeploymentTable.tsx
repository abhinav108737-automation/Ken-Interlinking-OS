import Link from "next/link";

import type { DeploymentView } from "@/lib/api-client";
import { deploymentHref, describeDeploymentErrorCode, describeDeploymentStatus } from "@/lib/deployments";
import { formatDateTime } from "@/lib/format";
import { describeActor } from "@/lib/recommendations";

import { StatusBadge } from "../StatusBadge";
import { AutonomousBadge } from "./AutonomousBadge";
import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const LINK = "text-blue-800 underline underline-offset-2";

export interface DeploymentTableProps {
  readonly deployments: readonly DeploymentView[];
  readonly caption: string;
  readonly total?: number;
  readonly emptyMessage?: string;
  /** Show the source page column (history); off on a plan's own panel. */
  readonly showSource?: boolean;
}

/** Deployments, in API order (newest first). Every value is rendered as React text. */
export function DeploymentTable({
  deployments,
  caption,
  total,
  emptyMessage = "No deployments yet.",
  showSource = true,
}: DeploymentTableProps) {
  const columns = ["Requested", "Status", ...(showSource ? ["Source page"] : []), "Links", "CMS page", "Requested by", "Error"];
  return (
    <TableFrame caption={caption} {...(total === undefined ? {} : { count: total })}>
      <HeaderRow columns={columns} />
      <tbody>
        {deployments.length === 0 ? (
          <EmptyRow colSpan={columns.length}>{emptyMessage}</EmptyRow>
        ) : (
          deployments.map((row) => {
            const actor = describeActor(row.actor);
            return (
              <tr key={row.id} className={ROW}>
                <th scope="row" className={`${CELL} whitespace-nowrap font-medium`}>
                  <Link href={deploymentHref(row.id)} className={LINK}>
                    <time dateTime={row.created_at}>{formatDateTime(row.created_at)}</time>
                    <span className="sr-only"> — view deployment</span>
                  </Link>
                </th>
                <td className={CELL}>
                  <StatusBadge presentation={describeDeploymentStatus(row.status)} />{" "}
                  <AutonomousBadge deployment={row} />
                </td>
                {showSource && (
                  <td className={CELL}>
                    <Link href={pageHref(row.source_page_id)} className={`${LINK} break-all font-mono text-xs`}>
                      {row.source_url}
                    </Link>
                  </td>
                )}
                <td className={NUM_CELL}>{row.plan_ids.length}</td>
                <td className={CELL}>
                  <span className="block text-xs text-slate-700">{row.cms_provider}</span>
                  <code className="break-all text-xs">{row.cms_page_id}</code>
                </td>
                <td className={CELL}>
                  <span className="text-xs text-slate-700">{actor.label}: </span>
                  <span className="break-words">{actor.id}</span>
                </td>
                <td className={CELL}>
                  {row.error_code === null || row.error_code === undefined ? (
                    <span className="text-slate-600">—</span>
                  ) : (
                    <span>
                      {describeDeploymentErrorCode(row.error_code)}{" "}
                      <code className="text-xs text-slate-600">{row.error_code}</code>
                    </span>
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
