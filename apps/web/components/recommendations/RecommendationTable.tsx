import Link from "next/link";

import type { LinkPlan } from "@/lib/api-client";
import { formatDateTime } from "@/lib/format";
import { describeRelationshipType } from "@/lib/intelligence";
import { pageDisplayTitle } from "@/lib/inventory";
import { describeLinkPlanStatus, describeReasonCode, planHref, topReasonCodes } from "@/lib/recommendations";

import { StatusBadge } from "../StatusBadge";
import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "../inventory/Table";
import { PlanScore } from "./PlanScore";

const LINK = "break-words text-blue-800 underline underline-offset-2";

export interface RecommendationTableProps {
  readonly plans: readonly LinkPlan[];
  readonly total: number;
  readonly caption?: string;
  readonly emptyMessage?: string;
  /**
   * `full` (the recommendation list) or a compact table for one page's detail
   * screen: `from` (plans whose source is the page) or `to` (whose target is).
   */
  readonly variant?: "full" | "from" | "to";
}

function TargetCell({ plan }: { readonly plan: LinkPlan }) {
  return (
    <td className={CELL}>
      <Link href={pageHref(plan.target_page_id)} className={LINK}>
        {pageDisplayTitle({ title: plan.target_title, canonical_url: plan.target_url })}
      </Link>
      <span className="block break-all font-mono text-xs text-slate-600">{plan.target_url}</span>
    </td>
  );
}

function SourceCell({ plan }: { readonly plan: LinkPlan }) {
  return (
    <td className={CELL}>
      <Link href={pageHref(plan.source_page_id)} className={`${LINK} font-mono text-xs`}>
        {plan.source_url}
      </Link>
      <span className="block text-xs text-slate-600">version {plan.source_page_version}</span>
    </td>
  );
}

/**
 * Link plans, in API order (highest score first). All page and anchor text is
 * rendered as React text; the anchor links to the plan's detail screen.
 */
export function RecommendationTable({
  plans,
  total,
  caption = "Recommendations",
  emptyMessage = "No recommendations.",
  variant = "full",
}: RecommendationTableProps) {
  const full = variant === "full";
  const columns = [
    "Score",
    "Anchor text",
    ...(variant !== "from" ? ["Source page"] : []),
    ...(variant !== "to" ? ["Target page"] : []),
    ...(full ? ["Relationship", "Top reasons"] : []),
    "Status",
    "Created",
  ];
  return (
    <TableFrame caption={caption} count={total}>
      <HeaderRow columns={columns} />
      <tbody>
        {plans.length === 0 ? (
          <EmptyRow colSpan={columns.length}>{emptyMessage}</EmptyRow>
        ) : (
          plans.map((plan) => {
            const reasons = topReasonCodes(plan.reason_codes);
            return (
              <tr key={plan.id} className={ROW}>
                <td className={CELL}>
                  <PlanScore plan={plan} />
                </td>
                <th scope="row" className={`${CELL} font-medium`}>
                  <Link href={planHref(plan.id)} className={LINK}>
                    {plan.anchor_text}{" "}
                    <span className="sr-only">— view recommendation</span>
                  </Link>
                </th>
                {variant !== "from" && <SourceCell plan={plan} />}
                {variant !== "to" && <TargetCell plan={plan} />}
                {full && (
                  <td className={CELL}>
                    {plan.relationship_type === null ? (
                      <span className="text-slate-600">None</span>
                    ) : (
                      describeRelationshipType(plan.relationship_type)
                    )}
                  </td>
                )}
                {full && (
                  <td className={CELL}>
                    {reasons.shown.length === 0 ? (
                      <span className="text-slate-600">—</span>
                    ) : (
                      <ul className="space-y-0.5 text-xs">
                        {reasons.shown.map((code) => (
                          <li key={code}>{describeReasonCode(code)}</li>
                        ))}
                        {reasons.hidden > 0 && <li className="text-slate-600">+{reasons.hidden} more</li>}
                      </ul>
                    )}
                  </td>
                )}
                <td className={CELL}>
                  <StatusBadge presentation={describeLinkPlanStatus(plan.status)} />
                </td>
                <td className={`${CELL} whitespace-nowrap`}>
                  <time dateTime={plan.created_at}>{formatDateTime(plan.created_at)}</time>
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
