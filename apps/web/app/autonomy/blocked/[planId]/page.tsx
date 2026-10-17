import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { AutonomyModeBanner } from "@/components/autonomy/AutonomyModeBanner";
import { BlockList } from "@/components/autonomy/BlockList";
import { OverridePanel } from "@/components/autonomy/OverridePanel";
import { PublishAttemptTable } from "@/components/autonomy/PublishAttemptTable";
import { ShadowNotice } from "@/components/autonomy/ShadowBadge";
import { ExternalUrl } from "@/components/inventory/ExternalUrl";
import { pageHref } from "@/components/inventory/PageInventoryTable";
import { PlacementPreviewPanel, SourceBlockText } from "@/components/recommendations/PlacementPreview";
import { GraphSignalsPanel } from "@/components/recommendations/GraphSignalsPanel";
import { ScoreBreakdownTable } from "@/components/recommendations/ScoreBreakdownTable";
import { TransitionHistory } from "@/components/recommendations/TransitionHistory";
import {
  getAutonomyMode,
  getLinkPlan,
  isMissingResourceError,
  listAutonomyDecisions,
  listBlockedActions,
  listPublishAttempts,
  requireSessionUser,
} from "@/lib/api";
import { can, describeRole, permissionsOf } from "@/lib/auth";
import {
  BLOCKED_PATH,
  blockKindOf,
  blockReasonText,
  describeAutonomyDecision,
  describeCriticOutcome,
  describeDecisionConfidence,
  describeRiskTier,
  describeShadowDecision,
  describeVerificationProfile,
  planAttemptsHref,
} from "@/lib/autonomy";
import { formatDateTime } from "@/lib/format";
import { describeRelationshipType } from "@/lib/intelligence";
import { pageDisplayTitle } from "@/lib/inventory";
import { describeLinkPlanStatus, describeReasonCode, isUuid, planHref } from "@/lib/recommendations";

import { overrideAction } from "../../actions";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Blocked action",
};

const FACTS = "grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm";
const LINK = "text-blue-800 underline underline-offset-2";

function Fact({ term, children }: { readonly term: string; readonly children: ReactNode }) {
  return (
    <>
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd className="min-w-0 break-words text-slate-900">{children}</dd>
    </>
  );
}

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

const ATTEMPT_LIMIT = 20;
/** `/autonomy/blocked` has no per-plan route, so the reason is looked up in its newest page. */
const BLOCKED_LOOKUP = 100;

/**
 * The investigation view for one blocked (or otherwise decided) plan: what the
 * autonomy engine decided and why, the deterministic blocks that fired with
 * what to check for each, the plan's own evidence (anchor, score breakdown,
 * destination graph position), its publish-attempt history, and — for an
 * operator holding `deploy` — the override control.
 *
 * It is reached from the blocked list, but it renders for any plan id, because
 * the useful question is "why did this not publish?" whatever state it is in.
 */
export default async function BlockedActionDetailPage({
  params,
}: {
  readonly params: Promise<{ planId: string }>;
}) {
  const { planId } = await params;
  if (!isUuid(planId)) notFound();

  const [planResult, decisionsResult, attemptsResult, blockedResult, health, me] = await Promise.all([
    getLinkPlan(planId),
    listAutonomyDecisions({ plan_id: planId, limit: 5 }),
    listPublishAttempts({ plan_id: planId, limit: ATTEMPT_LIMIT }),
    listBlockedActions({ limit: BLOCKED_LOOKUP }),
    getAutonomyMode(),
    requireSessionUser(),
  ]);

  if (!planResult.ok) {
    if (isMissingResourceError(planResult.error)) notFound();
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Blocked action</h1>
        <ApiErrorNotice error={planResult.error} what="Recommendation" />
        <p>
          <Link href={BLOCKED_PATH} className="text-blue-800 underline underline-offset-4">
            Back to blocked actions
          </Link>
        </p>
      </div>
    );
  }

  const plan = planResult.data;
  const status = describeLinkPlanStatus(plan.status);
  const user = me.ok ? me.data : null;
  const canOverride = can(permissionsOf(user), "deploy");
  // Newest first, so the first row is the decision in force.
  const latest = decisionsResult.ok ? (decisionsResult.data.items[0] ?? null) : null;
  const shadow = latest === null ? null : describeShadowDecision(latest);
  const blocks = latest?.blocks ?? [];
  const blockedRow = blockedResult.ok ? blockedResult.data.items.find((item) => item.plan_id === plan.id) : undefined;
  const reason = blockReasonText(blockedRow?.reason);
  // A BLOCK decision with no codes is a guarded deferral, not a hard block.
  const deferral = latest !== null && latest.decision === "block" && blockKindOf(blocks) === "deferral";
  // An operator has acted when a `user:` actor appears in the plan's transitions.
  const operatorActed = plan.transitions.some((item) => item.actor.startsWith("user:"));

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <p className="text-sm">
          <Link href={BLOCKED_PATH} className="text-blue-800 underline underline-offset-4">
            ← Blocked actions
          </Link>
        </p>
        <h1 className="break-words text-2xl font-semibold">Why “{plan.anchor_text}” did not publish</h1>
        <AutonomyModeBanner health={health.ok ? health.data : null} compact />
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge presentation={status} />
          {latest !== null && <StatusBadge presentation={describeAutonomyDecision(latest.decision)} />}
          {latest !== null && <StatusBadge presentation={describeRiskTier(latest.risk_tier)} />}
        </div>
        <p className="max-w-prose text-sm text-slate-800">{status.description}</p>
        {latest !== null && shadow !== null && (
          <ShadowNotice mode={latest.mode} wouldHave={shadow.wouldHave} />
        )}
      </header>

      <Section id="decision" title="The autonomy decision">
        {!decisionsResult.ok ? (
          <ApiErrorNotice error={decisionsResult.error} what="Autonomy decision" />
        ) : latest === null ? (
          <p role="status" className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-slate-800">
            No autonomy decision has been recorded for this plan. It has not reached the autonomy step yet.
          </p>
        ) : (
          <dl className={FACTS}>
            <Fact term="Decision">
              {describeAutonomyDecision(latest.decision).label}
              <span className="block text-xs text-slate-700">
                {describeAutonomyDecision(latest.decision).description}
              </span>
            </Fact>
            <Fact term="Mode in force then">
              {latest.mode}
              {shadow?.shadow === true && (
                <span className="block text-xs font-semibold text-amber-900">
                  Nothing was published. This record is a counterfactual.
                </span>
              )}
            </Fact>
            <Fact term="Would have been">
              {latest.would_have_decision === null ? (
                <span className="text-slate-600">— (not a shadow evaluation)</span>
              ) : (
                describeAutonomyDecision(latest.would_have_decision).label
              )}
            </Fact>
            <Fact term="Risk tier">{describeRiskTier(latest.risk_tier).label}</Fact>
            <Fact term="Critic outcome">
              {describeCriticOutcome(latest.critic_outcome).label}
              {latest.critic_verdict !== null && (
                <span className="ml-2 text-xs text-slate-700">
                  (model verdict: {latest.critic_verdict})
                </span>
              )}
            </Fact>
            <Fact term="Confidence">
              {(() => {
                const confidence = describeDecisionConfidence(latest);
                return (
                  <>
                    <span className="tabular-nums">{confidence.value}</span>{" "}
                    <span className="text-xs text-slate-700">({confidence.qualifier})</span>
                    <span className="block text-xs text-slate-700">{confidence.description}</span>
                  </>
                );
              })()}
            </Fact>
            <Fact term="Verification profile">
              {describeVerificationProfile(latest.verification_profile)}
            </Fact>
            <Fact term="Rollback available">
              {latest.rollback_available ? (
                "Yes — an inverse splice is possible"
              ) : (
                <span className="font-medium text-red-900">
                  No — nothing is published that cannot be undone
                </span>
              )}
            </Fact>
            <Fact term="Policy version">{latest.policy_version}</Fact>
            <Fact term="Engine version">
              <code className="text-xs">{latest.engine_version}</code>
            </Fact>
            <Fact term="Evaluated">
              <time dateTime={latest.evaluated_at}>{formatDateTime(latest.evaluated_at)}</time>
            </Fact>
          </dl>
        )}
      </Section>

      <Section id="blocks" title="Why it is blocked">
        <p className="max-w-prose text-sm text-slate-700">
          A hard block is computed from stored facts by code alone — no model call and no judgement — and cannot be
          overridden into a publish. A deferral has no block code: guarded mode held the plan back as too risky,
          uncalibrated or under-confident, and an operator may still override it.
        </p>
        <BlockList blocks={blocks} reason={reason} linkToFilter />
        {reason === null && (
          <p className="text-xs text-slate-700">
            No recorded reason was found among the {BLOCKED_LOOKUP} newest blocked actions.
          </p>
        )}
      </Section>

      <Section id="pages" title="Pages">
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="space-y-1 rounded border border-slate-300 p-3">
            <h3 className="font-semibold">Source page (link would go here)</h3>
            <p>
              <Link href={pageHref(plan.source_page_id)} className={`${LINK} break-words`}>
                {pageDisplayTitle({ title: plan.source_title, canonical_url: plan.source_url })}
              </Link>
            </p>
            <ExternalUrl url={plan.source_url} />
            <p className="text-xs text-slate-700">Pinned to version {plan.source_page_version}</p>
          </div>
          <div className="space-y-1 rounded border border-slate-300 p-3">
            <h3 className="font-semibold">Destination page (link would point here)</h3>
            <p>
              <Link href={pageHref(plan.target_page_id)} className={`${LINK} break-words`}>
                {pageDisplayTitle({ title: plan.target_title, canonical_url: plan.target_url })}
              </Link>
            </p>
            <ExternalUrl url={plan.target_url} />
            <p className="text-xs text-slate-700">Pinned to version {plan.target_page_version}</p>
          </div>
        </div>
      </Section>

      <Section id="placement" title="Proposed placement">
        <p className="text-sm text-slate-700">
          The anchor is copied verbatim from the source block and highlighted below. Text only — no page HTML is
          rendered.
        </p>
        <PlacementPreviewPanel plan={plan} />
        <SourceBlockText plan={plan} />
      </Section>

      <Section id="rationale" title="Anchor rationale">
        <p className="max-w-prose whitespace-pre-wrap break-words">{plan.rationale}</p>
        {plan.reason_codes.length === 0 ? (
          <p className="text-slate-700">No reason codes were recorded.</p>
        ) : (
          <ul aria-label="Reason codes" className="list-disc space-y-0.5 pl-5 text-sm">
            {plan.reason_codes.map((code) => (
              <li key={code}>
                {describeReasonCode(code)} <span className="font-mono text-xs text-slate-600">{code}</span>
              </li>
            ))}
          </ul>
        )}
        <dl className={FACTS}>
          <Fact term="Relationship">
            {plan.relationship_type === null ? (
              <span className="text-slate-600">None (no typed relationship)</span>
            ) : (
              describeRelationshipType(plan.relationship_type)
            )}
          </Fact>
        </dl>
      </Section>

      <Section id="score" title="Score breakdown">
        <ScoreBreakdownTable score={plan.score} />
      </Section>

      <Section id="graph" title="Destination in the link graph">
        <GraphSignalsPanel signals={plan.target_graph_signals} />
      </Section>

      <Section id="attempts" title="Publish attempts">
        {attemptsResult.ok ? (
          <>
            <PublishAttemptTable
              attempts={attemptsResult.data.items}
              total={attemptsResult.data.page.total}
              caption="Publish attempts for this plan"
            />
            <p className="text-sm">
              <Link href={planAttemptsHref(plan.id)} className="text-blue-800 underline underline-offset-4">
                All attempts for this plan
              </Link>
            </p>
          </>
        ) : (
          <ApiErrorNotice error={attemptsResult.error} what="Publish attempts" />
        )}
      </Section>

      <Section id="history" title="Status history">
        <TransitionHistory transitions={plan.transitions} />
      </Section>

      {canOverride ? (
        <OverridePanel
          planId={plan.id}
          expectedStatus={plan.status}
          blocks={blocks}
          deferral={deferral}
          action={overrideAction}
          alreadyOverridden={operatorActed}
        />
      ) : (
        <p className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
          {user === null
            ? "An operator override is unavailable because your session could not be confirmed."
            : `Recording an operator override needs the deploy permission. Your role (${describeRole(user.role)}) does not have it.`}
        </p>
      )}

      <p className="text-sm">
        <Link href={planHref(plan.id)} className="text-blue-800 underline underline-offset-4">
          Full recommendation detail
        </Link>
      </p>
    </div>
  );
}
