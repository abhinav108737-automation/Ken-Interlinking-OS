import Link from "next/link";
import type { ReactNode } from "react";

import type { AutonomyHealth, AutonomySummary, DashboardSummary } from "@/lib/api-client";
import { describeAgentOutcome } from "@/lib/agents";
import {
  ATTEMPTS_PATH,
  AUTONOMY_PATH,
  BLOCKED_PATH,
  DECISIONS_PATH,
  blockedCodeHref,
  describeAutonomyDecision,
  describeAutonomyModeShort,
  describeBlockCode,
  isKnownBlockCode,
  sortBlockCounts,
} from "@/lib/autonomy";
import { formatCount } from "@/lib/format";
import { sortCounts } from "@/lib/intelligence";
import { buildHref } from "@/lib/inventory";
import { RECOMMENDATIONS_PATH, describeOpportunityKind } from "@/lib/recommendations";
import { describeReviewAction } from "@/lib/review";

import { AutonomyModeBanner } from "../autonomy/AutonomyModeBanner";
import { LifecycleStatePanel } from "../autonomy/LifecycleStatePanel";
import { CountTable } from "../intelligence/CountTable";
import { JobRunTable } from "../jobs/JobRunTable";
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

export interface DashboardViewProps {
  readonly summary: DashboardSummary;
  /** `GET /autonomy`, or `null` when that call failed. */
  readonly autonomy: AutonomySummary | null;
  /** `GET /autonomy/mode`, or `null` when that call failed. */
  readonly health: AutonomyHealth | null;
  /** True when the session may act on an exception (`review`). */
  readonly canReview: boolean;
}

/**
 * The operator's home screen. Organised around the operational states of
 * ADR-0018 §3 — proposed, blocked, auto-published, verification pending,
 * verified, rolled back, failed — and NOT around an approval queue.
 *
 * Human approval appears only once, under "Operator exceptions", and is
 * labelled as an exception rather than as the day's work.
 */
export function DashboardView({ summary, autonomy, health, canReview }: DashboardViewProps) {
  const cards: readonly (readonly [string, string])[] = [
    ["Pages (all)", formatCount(summary.pages_total)],
    ["Live pages", formatCount(summary.pages_live)],
    ["Analysed (current)", formatCount(summary.pages_analyzed)],
    ["Protected pages", formatCount(summary.pages_protected)],
  ];
  const blockedCount = autonomy?.blocked_plans ?? 0;
  const exceptions = summary.awaiting_approval;
  return (
    <div className="space-y-8">
      <AutonomyModeBanner health={health} />

      {autonomy !== null && (
        <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded border border-slate-300 bg-white p-3">
            <dt className="text-sm text-slate-700">Auto-published</dt>
            <dd className="text-2xl font-semibold tabular-nums">
              <Link href={ATTEMPTS_PATH} className="text-blue-800 underline underline-offset-4">
                {formatCount(autonomy.auto_published)}
              </Link>
            </dd>
          </div>
          <div className="rounded border border-slate-300 bg-white p-3">
            <dt className="text-sm text-slate-700">Blocked — need investigation</dt>
            <dd className="text-2xl font-semibold tabular-nums">
              <Link href={BLOCKED_PATH} className="text-blue-800 underline underline-offset-4">
                {formatCount(autonomy.blocked_plans)}
              </Link>
            </dd>
          </div>
          <div className="rounded border border-slate-300 bg-white p-3">
            <dt className="text-sm text-slate-700">Rolled back</dt>
            <dd className="text-2xl font-semibold tabular-nums">
              <Link href={ATTEMPTS_PATH} className="text-blue-800 underline underline-offset-4">
                {formatCount(autonomy.rolled_back)}
              </Link>
            </dd>
          </div>
          <div className="rounded border border-slate-300 bg-white p-3">
            <dt className="text-sm text-slate-700">Mode in force</dt>
            <dd className="text-2xl font-semibold">{describeAutonomyModeShort(autonomy.mode)}</dd>
          </div>
        </dl>
      )}

      {blockedCount > 0 && (
        <div className="flex flex-wrap items-center gap-4 rounded border-2 border-red-700 bg-red-50 p-4">
          <p className="text-2xl font-semibold tabular-nums text-red-950">{formatCount(blockedCount)}</p>
          <p className="text-red-950">
            {blockedCount === 1 ? "plan was blocked" : "plans were blocked"} by a deterministic safety rule and never
            published.
          </p>
          <Link
            href={BLOCKED_PATH}
            className="rounded border border-red-800 bg-red-800 px-3 py-1 font-medium text-white hover:bg-red-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700"
          >
            Investigate blocked actions
          </Link>
        </div>
      )}

      <Section id="states" title="Link plans by operational state">
        <p className="max-w-prose text-sm text-slate-700">
          Publishing is policy-governed and autonomous: there is no approval step in the normal workflow. These are the
          states the system actually speaks in.
        </p>
        <LifecycleStatePanel counts={summary.plans_by_status} />
      </Section>

      {autonomy !== null && (
        <Section id="autonomy" title="Autonomy decisions">
          <div className="grid gap-4 lg:grid-cols-3">
            <CountTable
              caption="Decisions by decision"
              keyHeader="Decision"
              valueHeader="Decisions"
              rows={sortCounts(autonomy.decisions_by_decision).map(({ key, count }) => ({
                key,
                label: describeAutonomyDecision(key).label,
                count,
                href: buildHref(DECISIONS_PATH, { decision: key }),
              }))}
              emptyMessage="No autonomy decision recorded yet."
            />
            <CountTable
              caption="Decisions by mode"
              keyHeader="Mode"
              valueHeader="Decisions"
              rows={sortCounts(autonomy.decisions_by_mode).map(({ key, count }) => ({
                key,
                label:
                  key === "shadow" ? "Shadow (counterfactual — nothing published)" : describeAutonomyModeShort(key),
                count,
                href: buildHref(DECISIONS_PATH, { mode: key }),
              }))}
              emptyMessage="No autonomy decision recorded yet."
            />
            <CountTable
              caption="Deterministic blocks by code"
              keyHeader="Block code"
              valueHeader="Plans"
              rows={sortBlockCounts(autonomy.blocks_by_code).map(({ key, count }) => ({
                key,
                label: describeBlockCode(key),
                count,
                ...(isKnownBlockCode(key) ? { href: blockedCodeHref(key) } : {}),
              }))}
              emptyMessage="No deterministic block has fired."
            />
          </div>
          <p className="text-sm">
            <Link href={AUTONOMY_PATH} className="text-blue-800 underline underline-offset-4">
              Autonomy operations
            </Link>
          </p>
        </Section>
      )}

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

      <Section id="exceptions" title="Operator exceptions">
        <p className="max-w-prose text-sm text-slate-700">
          Approval is not part of the normal workflow. A plan reaches an operator only when guarded mode defers it, or
          when someone opens it as an exception.
        </p>
        <div className="flex flex-wrap items-center gap-4 rounded border border-slate-400 bg-slate-50 p-4">
          <p className="text-xl font-semibold tabular-nums">{formatCount(exceptions)}</p>
          <p className="text-slate-900">
            {exceptions === 1 ? "plan is" : "plans are"} deferred to an operator.
          </p>
          <Link
            href={buildHref(RECOMMENDATIONS_PATH, { status: "awaiting_approval" })}
            className="rounded border border-slate-500 bg-white px-3 py-1 text-slate-900 hover:bg-slate-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700"
          >
            {canReview ? "Handle exceptions" : "View exceptions"}
          </Link>
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <CountTable
            caption="Review actions (all time)"
            keyHeader="Action"
            valueHeader="Actions"
            rows={sortCounts(summary.reviews_by_action).map(({ key, count }) => ({
              key,
              label: describeReviewAction(key),
              count,
            }))}
            emptyMessage="No review action yet."
          />
          <CountTable
            caption="Opportunities by kind"
            keyHeader="Kind"
            valueHeader="Pages"
            rows={sortCounts(summary.opportunities_by_kind).map(({ key, count }) => ({
              key,
              label: describeOpportunityKind(key),
              count,
            }))}
            emptyMessage="No opportunities computed yet."
          />
        </div>
      </Section>

      <Section id="agents" title="Agent runs">
        <CountTable
          caption="Agent runs by outcome (all time)"
          keyHeader="Outcome"
          valueHeader="Runs"
          rows={sortCounts(summary.agent_runs_by_outcome).map(({ key, count }) => ({
            key,
            label: describeAgentOutcome(key).label,
            count,
          }))}
          emptyMessage="No agent runs yet."
        />
      </Section>

      <Section id="recent-reviews" title="Recent operator actions">
        <ReviewActionTable
          actions={summary.recent_review_actions}
          caption="Latest operator actions on exceptions"
          emptyMessage="No operator has acted on an exception yet."
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
