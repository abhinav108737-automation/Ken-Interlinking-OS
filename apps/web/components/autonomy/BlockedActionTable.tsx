import Link from "next/link";

import type { BlockedActionView } from "@/lib/api-client";
import { blockedPlanHref, describeBlockCode, describeRiskTier, isKnownBlockCode } from "@/lib/autonomy";
import { formatDateTime } from "@/lib/format";
import { planHref } from "@/lib/recommendations";

import { StatusBadge } from "../StatusBadge";
import { ModeCell } from "./ShadowBadge";
import { ExternalUrl } from "../inventory/ExternalUrl";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Blocks that fired", "Mode", "Risk", "Anchor", "Source → destination", "Decided", "Investigate"] as const;

const OVERRIDDEN: Parameters<typeof StatusBadge>[0]["presentation"] = {
  label: "Operator acted",
  description: "An operator has already overridden or confirmed this block. The action is recorded in the audit log.",
  tone: "neutral",
  known: true,
};

/**
 * Blocked actions (`GET /autonomy/blocked`) — the screen that replaces the
 * approval inbox. Nothing here is waiting for a human to approve it: these are
 * exceptions that a deterministic rule refused, listed so an operator can
 * investigate them.
 */
export function BlockedActionTable({
  blocked,
  total,
  caption = "Blocked actions",
}: {
  readonly blocked: readonly BlockedActionView[];
  readonly total: number;
  readonly caption?: string;
}) {
  return (
    <TableFrame caption={caption} count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {blocked.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>
            No blocked action. Every evaluated plan either published or was not worth doing.
          </EmptyRow>
        ) : (
          blocked.map((item) => (
            <tr key={item.plan_id} className={ROW}>
              <th scope="row" className={`${CELL} font-medium`}>
                {item.blocks.length === 0 ? (
                  <span className="text-slate-700">No code recorded</span>
                ) : (
                  <ul className="list-none space-y-0.5">
                    {item.blocks.map((code) => (
                      <li key={code}>
                        {describeBlockCode(code)}
                        <span className="ml-2 font-mono text-xs font-normal text-slate-600">{code}</span>
                        {!isKnownBlockCode(code) && (
                          <span className="ml-1 text-xs font-normal text-amber-900">(unknown to this console)</span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {item.overridden && (
                  <span className="mt-1 block">
                    <StatusBadge presentation={OVERRIDDEN} />
                  </span>
                )}
              </th>
              <td className={CELL}>
                <ModeCell mode={item.mode} />
              </td>
              <td className={CELL}>
                <StatusBadge presentation={describeRiskTier(item.risk_tier)} />
              </td>
              <td className={`${CELL} max-w-xs break-words`}>“{item.anchor_text}”</td>
              <td className={CELL}>
                <ExternalUrl url={item.source_url} />
                <span aria-hidden="true" className="mx-1">
                  →
                </span>
                <ExternalUrl url={item.target_url} />
              </td>
              <td className={CELL}>
                <time dateTime={item.decided_at} className="whitespace-nowrap text-xs">
                  {formatDateTime(item.decided_at)}
                </time>
              </td>
              <td className={CELL}>
                <Link href={blockedPlanHref(item.plan_id)} className="text-blue-800 underline underline-offset-2">
                  Investigate
                </Link>
                <Link
                  href={planHref(item.plan_id)}
                  className="mt-1 block text-xs text-blue-800 underline underline-offset-2"
                >
                  Recommendation
                </Link>
              </td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
