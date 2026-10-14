import Link from "next/link";
import type { ReactNode } from "react";

import type { DashboardSummary } from "@/lib/api-client";
import { describeAgentOutcome } from "@/lib/agents";
import { formatCount } from "@/lib/format";
import { sortCounts } from "@/lib/intelligence";
import { describeLinkPlanStatus, describeOpportunityKind, RECOMMENDATIONS_PATH, sortStatusCounts } from "@/lib/recommendations";
import { describeReviewAction } from "@/lib/review";
import { describeAutoApprove } from "@/lib/status";

import { StatusBadge } from "../StatusBadge";
import { CountTable } from "../intelligence/CountTable";
import { JobRunTable } from "../jobs/JobRunTable";
import { statusHref } from "../recommendations/RecommendationSummaryPanel";
import { ReviewActionTable } from "../review/ReviewActionTable";

function Section({ id, title, children }: { readonly id: string; readonly title: string; readonly children: ReactNode }) {
  return (
    <section aria-labelledby={`${id}-heading`} className="space-y-3">
      <h2 id={`${id}-heading`} className="text-xl font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

/** The reviewer's home screen (`GET /dashboard`). Text only; every count links to its list where one exists. */
export function DashboardView({ summary, canReview }: { readonly summary: DashboardSummary; readonly canReview: boolean }) {
  const autoApprove = describeAutoApprove(summary.auto_approve_enabled);
  const cards: readonly (readonly [string, string])[] = [
    ["Pages (all)", formatCount(summary.pages_total)],
    ["Live pages", formatCount(summary.pages_live)],
    ["Analysed (current)", formatCount(summary.pages_analyzed)],
    ["Protected pages", formatCount(summary.pages_protected)],
  ];
  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-center gap-4 rounded border border-amber-700 bg-amber-50 p-4">
        <p className="text-2xl font-semibold tabular-nums">{formatCount(summary.awaiting_approval)}</p>
        <p className="text-amber-950">
          {summary.awaiting_approval === 1 ? "recommendation is" : "recommendations are"} awaiting approval.
        </p>
        <Link
          href={RECOMMENDATIONS_PATH}
          className="rounded border border-blue-800 bg-blue-800 px-3 py-1 font-medium text-white hover:bg-blue-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700"
        >
          {canReview ? "Review the inbox" : "View the inbox"}
        </Link>
      </div>

      <p className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">AUTO_APPROVE:</span>
        <StatusBadge presentation={autoApprove} />
        <span className="text-slate-700">{autoApprove.description}</span>
      </p>

      <Section id="content" title="Content">
        <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {cards.map(([term, value]) => (
            <div key={term} className="rounded border border-slate-300 bg-white p-3">
              <dt className="text-sm text-slate-700">{term}</dt>
              <dd className="text-lg font-semibold tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
      </Section>

      <Section id="pipeline" title="Recommendations and reviews">
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
            caption="Review actions (all time)"
            keyHeader="Action"
            valueHeader="Actions"
            rows={sortCounts(summary.reviews_by_action).map(({ key, count }) => ({ key, label: describeReviewAction(key), count }))}
            emptyMessage="No review actions yet."
          />
          <CountTable
            caption="Opportunities by kind"
            keyHeader="Kind"
            valueHeader="Pages"
            rows={sortCounts(summary.opportunities_by_kind).map(({ key, count }) => ({ key, label: describeOpportunityKind(key), count }))}
            emptyMessage="No opportunities computed yet."
          />
          <CountTable
            caption="Agent runs by outcome (all time)"
            keyHeader="Outcome"
            valueHeader="Runs"
            rows={sortCounts(summary.agent_runs_by_outcome).map(({ key, count }) => ({ key, label: describeAgentOutcome(key).label, count }))}
            emptyMessage="No agent runs yet."
          />
        </div>
      </Section>

      <Section id="recent-reviews" title="Recent review actions">
        <ReviewActionTable
          actions={summary.recent_review_actions}
          caption="Latest review actions"
          emptyMessage="No reviewer has acted yet."
        />
      </Section>

      <Section id="latest-jobs" title="Latest jobs">
        {summary.latest_jobs.length === 0 ? (
          <p role="status" className="text-slate-700">
            No batch job has run yet.
          </p>
        ) : (
          <JobRunTable runs={summary.latest_jobs} total={summary.latest_jobs.length} />
        )}
      </Section>
    </div>
  );
}
