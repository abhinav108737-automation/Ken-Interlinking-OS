import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { AutonomyModeBanner } from "@/components/autonomy/AutonomyModeBanner";
import { BlockList } from "@/components/autonomy/BlockList";
import { OverridePanel } from "@/components/autonomy/OverridePanel";
import { PublishAttemptTable } from "@/components/autonomy/PublishAttemptTable";
import { ShadowNotice } from "@/components/autonomy/ShadowBadge";
import { ExternalUrl } from "@/components/inventory/ExternalUrl";
import { GraphSignalsPanel } from "@/components/recommendations/GraphSignalsPanel";
import { ScoreBreakdownTable } from "@/components/recommendations/ScoreBreakdownTable";
import {
  getAutonomyMode,
  getLinkPlan,
  isMissingResourceError,
  listAutonomyDecisions,
  listPublishAttempts,
  requireSessionUser,
} from "@/lib/api";
import { can, permissionsOf } from "@/lib/auth";
import {
  BLOCKED_PATH,
  describeAutonomyDecision,
  describeCriticOutcome,
  describeDecisionConfidence,
  describeRiskTier,
  describeShadowDecision,
  describeVerificationProfile,
} from "@/lib/autonomy";
import { isUuid } from "@/lib/recommendations";

import { overrideAction } from "../../actions";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Blocked action",
};

/** Attempts shown for this plan. One plan's ledger is short; this is a ceiling, not a page size. */
const ATTEMPT_LIMIT = 50;

function Section({
  id,
  title,
  children,
}: {
  readonly id: string;
  readonly title: string;
  readonly children: ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="space-y-3">
      <h2 id={id} className="text-xl font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Fact({ label, value, note }: { readonly label: string; readonly value: ReactNode; readonly note?: string }) {
  return (
    <div className="space-y-0.5">
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-700">{label}</dt>
      <dd className="text-sm text-slate-900">
        {value}
        {note !== undefined && <span className="block text-xs text-slate-700">{note}</span>}
      </dd>
    </div>
  );
}

/**
 * Why one plan did not publish (ADR-0018 §5, ADR-0019).
 *
 * This is an INVESTIGATION view, not an approval queue: it answers "what fired,
 * what does it mean, what should I check", and the only action it offers is the
 * operator override, which is an exception and needs the `deploy` permission.
 *
 * The decision record is read from `/autonomy/decisions` filtered to this plan
 * rather than from the plan itself, because a plan carries no decision: the
 * engine's record is a separate, immutable row. A plan with no decision row is
 * reported as exactly that instead of being presented as an implicit block.
 *
 * Every partial failure degrades to a notice: a missing attempt ledger or graph
 * reading must never hide the blocks, which are the reason the operator came.
 */
export default async function BlockedActionDetailPage({
  params,
}: {
  readonly params: Promise<{ planId: string }>;
}) {
  const { planId } = await params;
  // A malformed id is a 404 before any call: an id from the URL is never sent
  // to the API, so a traversal attempt cannot reach it.
  if (!isUuid(planId)) {
    notFound();
  }

  const [planResult, decisionsResult, attemptsResult, health, me] = await Promise.all([
    getLinkPlan(planId),
    listAutonomyDecisions({ plan_id: planId, limit: 1, offset: 0 }),
    listPublishAttempts({ plan_id: planId, limit: ATTEMPT_LIMIT, offset: 0 }),
    getAutonomyMode(),
    requireSessionUser(),
  ]);

  if (!planResult.ok) {
    if (isMissingResourceError(planResult.error)) {
      notFound();
    }
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Blocked action</h1>
        <ApiErrorNotice error={planResult.error} what="This plan" />
        <Link href={BLOCKED_PATH} className="underline underline-offset-2">
          Back to blocked actions
        </Link>
      </div>
    );
  }

  const plan = planResult.data;
  const decision = decisionsResult.ok ? (decisionsResult.data.items[0] ?? null) : null;
  const blocks = decision?.blocks ?? [];
  const canOverride = can(permissionsOf(me.ok ? me.data : null), "deploy");
  const shadow = decision === null ? null : describeShadowDecision(decision);
  const confidence = decision === null ? null : describeDecisionConfidence(decision);

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <p className="text-sm">
          <Link href={BLOCKED_PATH} className="underline underline-offset-2">
            Blocked actions
          </Link>
        </p>
        <h1 className="text-2xl font-semibold">Why “{plan.anchor_text}” did not publish</h1>
        <AutonomyModeBanner health={health.ok ? health.data : null} compact />
        <p className="max-w-prose text-slate-800">
          The planned link from <ExternalUrl url={plan.source_url} /> to <ExternalUrl url={plan.target_url} />. Nothing
          on the site was changed by the decision below.
        </p>
        {shadow !== null && <ShadowNotice mode={decision!.mode} wouldHave={shadow.wouldHave} />}
        {decision !== null && !decision.rollback_available && (
          <p role="note" className="max-w-prose rounded border-2 border-red-800 bg-red-50 p-3 text-sm text-red-950">
            <span className="font-semibold">Rollback is not available for this plan.</span> An inverse splice could not
            be computed, so publishing it could not be undone. The engine blocks it for that reason alone: nothing is
            published that cannot be undone.
          </p>
        )}
      </header>

      <Section id="decision-heading" title="The autonomy decision">
        {decision === null ? (
          <p role="note" className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
            No autonomy decision has been recorded for this plan. Either the engine has not evaluated it yet, or the
            record was written by a build that did not keep one. Nothing was published.
          </p>
        ) : (
          <>
            <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <Fact
                label="Decision"
                value={describeAutonomyDecision(decision.decision).label}
                note={describeAutonomyDecision(decision.decision).description}
              />
              <Fact label="Risk tier" value={describeRiskTier(decision.risk_tier).label} />
              <Fact
                label="Confidence"
                value={`${confidence!.value} (${confidence!.qualifier})`}
                note={confidence!.description}
              />
              <Fact label="Critic outcome" value={describeCriticOutcome(decision.critic_outcome).label} />
              <Fact label="Verification profile" value={describeVerificationProfile(decision.verification_profile)} />
              <Fact
                label="Rollback"
                value={
                  decision.rollback_available
                    ? "Yes — an inverse splice is possible"
                    : "No — an inverse splice is not possible"
                }
              />
              <Fact label="Policy version" value={String(decision.policy_version)} />
              <Fact label="Engine version" value={decision.engine_version} />
              <Fact label="Evaluated at" value={decision.evaluated_at} />
            </dl>
            {!decisionsResult.ok && <ApiErrorNotice error={decisionsResult.error} what="The autonomy decision" />}
          </>
        )}
        {!decisionsResult.ok && decision === null && (
          <ApiErrorNotice error={decisionsResult.error} what="The autonomy decision" />
        )}
      </Section>

      <Section id="blocks-heading" title="Deterministic blocks that fired">
        <p className="max-w-prose text-sm text-slate-800">
          Each block is computed from stored facts by code alone — no model call and no judgement — and is never
          overridable into a publish.
        </p>
        <BlockList blocks={blocks} linkToFilter />
      </Section>

      <Section id="score-heading" title="Score breakdown">
        <p className="max-w-prose text-sm text-slate-800">
          Every signal that produced this plan&apos;s score, with its value, weight and contribution — never collapsed
          into one opaque number.
        </p>
        <ScoreBreakdownTable score={plan.score} />
      </Section>

      <Section id="rationale-heading" title="Anchor rationale">
        <p className="max-w-prose text-slate-900">{plan.rationale}</p>
      </Section>

      <Section id="graph-heading" title="Destination in the link graph">
        <p className="max-w-prose text-sm text-slate-800">
          The destination&apos;s graph position, read at request time: the plan is immutable, but the graph moves under
          it.
        </p>
        <GraphSignalsPanel signals={plan.target_graph_signals} />
      </Section>

      <Section id="attempts-heading" title="Publish attempts">
        {attemptsResult.ok ? (
          <PublishAttemptTable
            attempts={attemptsResult.data.items}
            total={attemptsResult.data.page.total}
            caption="Publish attempts for this plan"
          />
        ) : (
          <ApiErrorNotice error={attemptsResult.error} what="Publish attempts" />
        )}
      </Section>

      {canOverride ? (
        <OverridePanel
          planId={planId}
          expectedStatus={plan.status}
          blocks={blocks}
          action={overrideAction}
          alreadyOverridden={false}
        />
      ) : (
        <p className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
          Recording an operator override needs the deploy permission, which your role does not have. You can still
          investigate every block on this page.
        </p>
      )}
    </div>
  );
}
