import Link from "next/link";

import type { AutonomyDecisionView } from "@/lib/api-client";
import {
  describeAutonomyDecision,
  describeBlockSet,
  describeDecisionConfidence,
  describeRiskTier,
  describeShadowDecision,
  describeVerificationProfile,
  isShadowMode,
} from "@/lib/autonomy";
import { formatDateTime } from "@/lib/format";
import { describeLinkPlanStatus, planHref } from "@/lib/recommendations";

import { StatusBadge } from "../StatusBadge";
import { ExternalUrl } from "../inventory/ExternalUrl";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = [
  "Decision",
  "Mode",
  "Plan",
  "Source → destination",
  "Risk and confidence",
  "Blocks",
  "Evaluated",
] as const;

/**
 * The recorded autonomy decision log (`GET /autonomy/decisions`). Every row
 * carries the mode it was taken in, so a shadow row — which did NOT happen — can
 * never be read as an action. In shadow the row also shows what would have
 * happened, labelled as a counterfactual.
 */
export function DecisionTable({
  decisions,
  total,
  caption = "Autonomy decisions",
}: {
  readonly decisions: readonly AutonomyDecisionView[];
  readonly total: number;
  readonly caption?: string;
}) {
  return (
    <TableFrame caption={caption} count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {decisions.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>No autonomy decision has been recorded yet.</EmptyRow>
        ) : (
          decisions.map((decision) => {
            const shadow = describeShadowDecision(decision);
            const confidence = describeDecisionConfidence(decision);
            return (
              <tr key={decision.id} className={ROW}>
                <th scope="row" className={`${CELL} font-medium`}>
                  <StatusBadge presentation={describeAutonomyDecision(decision.decision)} />
                  {shadow.shadow && (
                    <span className="mt-1 block text-xs font-normal text-amber-900">
                      Did not happen.
                      {shadow.wouldHave === null
                        ? " No counterfactual decision was recorded."
                        : ` Would have been: ${shadow.wouldHave}.`}
                    </span>
                  )}
                </th>
                <td className={CELL}>
                  {isShadowMode(decision.mode) ? (
                    <StatusBadge
                      presentation={{
                        label: "Shadow — did not happen",
                        description: shadow.description,
                        tone: "caution",
                        known: true,
                      }}
                    />
                  ) : (
                    <span className="whitespace-nowrap">{decision.mode}</span>
                  )}
                </td>
                <td className={CELL}>
                  <Link href={planHref(decision.plan_id)} className="font-mono text-xs text-blue-800 underline">
                    {decision.plan_id}
                  </Link>
                  <span className="mt-1 block">
                    <StatusBadge presentation={describeLinkPlanStatus(decision.plan_status)} />
                  </span>
                </td>
                <td className={CELL}>
                  <ExternalUrl url={decision.source_url} />
                  <span aria-hidden="true" className="mx-1">
                    →
                  </span>
                  <ExternalUrl url={decision.target_url} />
                </td>
                <td className={CELL}>
                  <StatusBadge presentation={describeRiskTier(decision.risk_tier)} />
                  <span className="mt-1 block text-xs text-slate-700" title={confidence.description}>
                    {confidence.value} ({confidence.qualifier})
                  </span>
                  <span className="block text-xs text-slate-700">
                    Verification: {describeVerificationProfile(decision.verification_profile)}
                    {decision.rollback_available ? "" : " · rollback not available"}
                  </span>
                </td>
                <td className={CELL}>
                  {decision.blocks.length === 0 ? (
                    <span className="text-slate-600">None</span>
                  ) : (
                    <StatusBadge presentation={describeBlockSet(decision.blocks)} />
                  )}
                </td>
                <td className={CELL}>
                  <time dateTime={decision.evaluated_at} className="whitespace-nowrap text-xs">
                    {formatDateTime(decision.evaluated_at)}
                  </time>
                  <span className="block text-xs text-slate-700">policy v{decision.policy_version}</span>
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
