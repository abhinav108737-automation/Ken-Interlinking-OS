import Link from "next/link";

import type { PublishAttemptView } from "@/lib/api-client";
import {
  describeAttemptOutcome,
  describeAutonomyDecision,
  describeVerificationProfile,
  inCooldown,
} from "@/lib/autonomy";
import { formatDateTime } from "@/lib/format";
import { planHref } from "@/lib/recommendations";

import { StatusBadge } from "../StatusBadge";
import { ModeCell } from "./ShadowBadge";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Outcome", "Mode", "Decision", "Plan", "Attempt", "Verification", "Occurred", "Retry"] as const;

/**
 * The append-only publish-attempt ledger (`GET /autonomy/attempts`): what was
 * auto-published, what was rolled back and what failed (ADR-0018 §4).
 *
 * `rolled_back` and `failure_code` are the rollback history; the cool-down
 * column is why a failing pair is not retried in a loop. A shadow attempt is
 * labelled as not having happened, like every other shadow record.
 */
export function PublishAttemptTable({
  attempts,
  total,
  caption = "Publish attempts",
  /** Injected in tests so the cool-down column is deterministic. */
  nowMs,
}: {
  readonly attempts: readonly PublishAttemptView[];
  readonly total: number;
  readonly caption?: string;
  readonly nowMs?: number;
}) {
  return (
    <TableFrame caption={caption} count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {attempts.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>
            No publish attempt recorded. Nothing has been written to the site by the autonomy engine.
          </EmptyRow>
        ) : (
          attempts.map((attempt) => {
            const cooling = inCooldown(attempt, nowMs);
            return (
              <tr key={attempt.id} className={ROW}>
                <th scope="row" className={CELL}>
                  <StatusBadge presentation={describeAttemptOutcome(attempt)} />
                  {attempt.failure_code !== null && (
                    <span className="mt-1 block font-mono text-xs font-normal text-slate-700">
                      {attempt.failure_code}
                    </span>
                  )}
                </th>
                <td className={CELL}>
                  <ModeCell mode={attempt.mode} />
                </td>
                <td className={CELL}>{describeAutonomyDecision(attempt.decision).label}</td>
                <td className={CELL}>
                  <Link href={planHref(attempt.plan_id)} className="font-mono text-xs text-blue-800 underline">
                    {attempt.plan_id}
                  </Link>
                </td>
                <td className={NUM_CELL}>{attempt.attempt}</td>
                <td className={CELL}>{describeVerificationProfile(attempt.verification_profile)}</td>
                <td className={CELL}>
                  <time dateTime={attempt.occurred_at} className="whitespace-nowrap text-xs">
                    {formatDateTime(attempt.occurred_at)}
                  </time>
                </td>
                <td className={CELL}>
                  {attempt.cooldown_until === null ? (
                    <span className="text-slate-600">No cool-down</span>
                  ) : (
                    <>
                      <span className="block text-xs">
                        {cooling ? "In cool-down until" : "Cool-down ended"}
                      </span>
                      <time dateTime={attempt.cooldown_until} className="whitespace-nowrap text-xs">
                        {formatDateTime(attempt.cooldown_until)}
                      </time>
                    </>
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
