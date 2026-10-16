import Link from "next/link";

import type { AutonomySummary } from "@/lib/api-client";
import {
  ATTEMPTS_PATH,
  BLOCKED_PATH,
  blockedCodeHref,
  describeAutonomyDecision,
  describeAutonomyModeShort,
  describeBlockCode,
  isKnownBlockCode,
  sortBlockCounts,
} from "@/lib/autonomy";
import { formatCount } from "@/lib/format";
import { sortCounts } from "@/lib/intelligence";

import { CountTable } from "../intelligence/CountTable";

/**
 * The operations overview (`GET /autonomy`). Counts only; the lists they link to
 * carry the detail. `mode` is the mode in force now, and is stated here as well
 * as in the banner because this is the screen an operator reads the numbers on.
 */
export function AutonomySummaryPanel({ summary }: { readonly summary: AutonomySummary }) {
  const cards: readonly (readonly [string, string, string | undefined])[] = [
    ["Auto-published", formatCount(summary.auto_published), ATTEMPTS_PATH],
    ["Blocked plans", formatCount(summary.blocked_plans), BLOCKED_PATH],
    ["Rolled back", formatCount(summary.rolled_back), ATTEMPTS_PATH],
    ["Mode in force", describeAutonomyModeShort(summary.mode), undefined],
  ];
  return (
    <div className="space-y-4">
      <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {cards.map(([term, value, href]) => (
          <div key={term} className="rounded border border-slate-300 bg-white p-3">
            <dt className="text-sm text-slate-700">{term}</dt>
            <dd className="text-lg font-semibold tabular-nums">
              {href === undefined ? (
                value
              ) : (
                <Link href={href} className="text-blue-800 underline underline-offset-4">
                  {value}
                </Link>
              )}
            </dd>
          </div>
        ))}
      </dl>
      <div className="grid gap-4 lg:grid-cols-3">
        <CountTable
          caption="Decisions by decision"
          keyHeader="Decision"
          valueHeader="Decisions"
          rows={sortCounts(summary.decisions_by_decision).map(({ key, count }) => ({
            key,
            label: describeAutonomyDecision(key).label,
            count,
          }))}
          emptyMessage="No autonomy decision recorded yet."
        />
        <CountTable
          caption="Decisions by mode"
          keyHeader="Mode"
          valueHeader="Decisions"
          rows={sortCounts(summary.decisions_by_mode).map(({ key, count }) => ({
            key,
            label:
              key === "shadow"
                ? "Shadow (counterfactual — nothing published)"
                : describeAutonomyModeShort(key),
            count,
          }))}
          emptyMessage="No autonomy decision recorded yet."
        />
        <CountTable
          caption="Deterministic blocks by code"
          keyHeader="Block code"
          valueHeader="Plans"
          rows={sortBlockCounts(summary.blocks_by_code).map(({ key, count }) => ({
            key,
            label: describeBlockCode(key),
            count,
            ...(isKnownBlockCode(key) ? { href: blockedCodeHref(key) } : {}),
          }))}
          emptyMessage="No deterministic block has fired."
        />
      </div>
    </div>
  );
}
