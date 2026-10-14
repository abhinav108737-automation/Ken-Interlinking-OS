import Link from "next/link";

import type { RecommendationSummary } from "@/lib/api-client";
import { formatCount } from "@/lib/format";
import { jobRunHref, sortCounts } from "@/lib/intelligence";
import { buildHref } from "@/lib/inventory";
import {
  ALL_STATUSES,
  DEFAULT_STATUS_FILTER,
  LINK_PLAN_STATUSES,
  OPPORTUNITIES_PATH,
  RECOMMENDATIONS_PATH,
  describeLinkPlanStatus,
  describeOpportunityKind,
  sortStatusCounts,
} from "@/lib/recommendations";

import { CountTable } from "../intelligence/CountTable";

function statusHref(status: string): string | undefined {
  if (!(LINK_PLAN_STATUSES as readonly string[]).includes(status)) return undefined;
  return status === DEFAULT_STATUS_FILTER ? RECOMMENDATIONS_PATH : buildHref(RECOMMENDATIONS_PATH, { status });
}

/** Total plans across every status. */
export function totalPlans(summary: Pick<RecommendationSummary, "plans_by_status">): number {
  return Object.values(summary.plans_by_status).reduce((sum, count) => sum + count, 0);
}

/** The recommendation dashboard tile: counts by status and kind, coverage and policy versions. */
export function RecommendationSummaryPanel({ summary }: { readonly summary: RecommendationSummary }) {
  const cards: readonly (readonly [string, string])[] = [
    ["Plans (all statuses)", formatCount(totalPlans(summary))],
    ["Source pages with plans", formatCount(summary.sources_with_plans)],
    ["Target pages with plans", formatCount(summary.targets_with_plans)],
    ["Recommendation policy", `version ${summary.policy_version}`],
    ["Ranking policy", `version ${summary.ranking_policy_version}`],
  ];
  return (
    <div className="space-y-4">
      <dl className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {cards.map(([term, value]) => (
          <div key={term} className="rounded border border-slate-300 bg-white p-3">
            <dt className="text-sm text-slate-700">{term}</dt>
            <dd className="text-lg font-semibold tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="text-sm text-slate-700">
        Last recommendation run:{" "}
        {summary.last_run_id === null ? (
          <span>none yet</span>
        ) : (
          <Link href={jobRunHref(summary.last_run_id)} className="font-mono text-xs text-blue-800 underline">
            {summary.last_run_id}
          </Link>
        )}
        {" · "}
        <Link href={buildHref(RECOMMENDATIONS_PATH, { status: ALL_STATUSES })} className="text-blue-800 underline">
          All recommendations
        </Link>
      </p>
      <div className="grid gap-4 lg:grid-cols-2">
        <CountTable
          caption="Plans by status"
          keyHeader="Status"
          valueHeader="Plans"
          rows={sortStatusCounts(summary.plans_by_status).map(({ key, count }) => {
            const href = statusHref(key);
            return { key, label: describeLinkPlanStatus(key).label, count, ...(href === undefined ? {} : { href }) };
          })}
          emptyMessage="No plans yet."
        />
        <CountTable
          caption="Opportunities by kind"
          keyHeader="Kind"
          valueHeader="Pages"
          rows={sortCounts(summary.opportunities_by_kind).map(({ key, count }) => ({
            key,
            label: describeOpportunityKind(key),
            count,
            href: `${OPPORTUNITIES_PATH}#${encodeURIComponent(key)}`,
          }))}
          emptyMessage="No opportunities computed yet."
        />
      </div>
    </div>
  );
}
