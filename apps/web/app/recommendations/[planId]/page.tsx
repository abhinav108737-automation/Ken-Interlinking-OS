import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { AgentPlanReviewPanel } from "@/components/agents/AgentPlanReviewPanel";
import { ExternalUrl } from "@/components/inventory/ExternalUrl";
import { pageHref } from "@/components/inventory/PageInventoryTable";
import { PlacementPreviewPanel, SourceBlockText } from "@/components/recommendations/PlacementPreview";
import { PlanScore } from "@/components/recommendations/PlanScore";
import { ScoreBreakdownTable } from "@/components/recommendations/ScoreBreakdownTable";
import { TransitionHistory } from "@/components/recommendations/TransitionHistory";
import { type LinkPlanDetail, getLinkPlan, isMissingResourceError, listAgentPlanReviews } from "@/lib/api";
import { formatDateTime, shortHash } from "@/lib/format";
import { describeRelationshipType, jobRunHref } from "@/lib/intelligence";
import { buildHref, pageDisplayTitle } from "@/lib/inventory";
import {
  CANDIDATES_PATH,
  RECOMMENDATIONS_PATH,
  describeLinkPlanStatus,
  describePlanAnchorSource,
  describeReasonCode,
  describeRetrievalChannel,
  planHref,
} from "@/lib/recommendations";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Recommendation detail",
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

function Optional({ value, none }: { readonly value: string | null; readonly none: string }) {
  return value === null || value.trim() === "" ? (
    <span className="text-slate-600">{none}</span>
  ) : (
    <code className="text-xs">{value}</code>
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

function PageFacts({ plan }: { readonly plan: LinkPlanDetail }) {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="space-y-1 rounded border border-slate-300 p-3">
        <h3 className="font-semibold">Source page (link goes here)</h3>
        <p>
          <Link href={pageHref(plan.source_page_id)} className={`${LINK} break-words`}>
            {pageDisplayTitle({ title: plan.source_title, canonical_url: plan.source_url })}
          </Link>
        </p>
        <ExternalUrl url={plan.source_url} />
        <p className="text-xs text-slate-700">Pinned to version {plan.source_page_version}</p>
      </div>
      <div className="space-y-1 rounded border border-slate-300 p-3">
        <h3 className="font-semibold">Target page (link points here)</h3>
        <p>
          <Link href={pageHref(plan.target_page_id)} className={`${LINK} break-words`}>
            {pageDisplayTitle({ title: plan.target_title, canonical_url: plan.target_url })}
          </Link>
        </p>
        <ExternalUrl url={plan.target_url} />
        <p className="text-xs text-slate-700">Pinned to version {plan.target_page_version}</p>
      </div>
    </div>
  );
}

export default async function RecommendationDetailPage({
  params,
}: {
  readonly params: Promise<{ planId: string }>;
}) {
  const { planId } = await params;
  // The agent review is fetched alongside; its failure never hides the plan.
  const [result, reviewsResult] = await Promise.all([getLinkPlan(planId), listAgentPlanReviews(planId)]);

  if (!result.ok) {
    if (isMissingResourceError(result.error)) {
      notFound();
    }
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Recommendation detail</h1>
        <ApiErrorNotice error={result.error} what="Recommendation" />
        <p>
          <Link href={RECOMMENDATIONS_PATH} className="text-blue-800 underline underline-offset-4">
            Back to recommendations
          </Link>
        </p>
      </div>
    );
  }

  const plan = result.data;
  const status = describeLinkPlanStatus(plan.status);
  const candidatesHref = buildHref(CANDIDATES_PATH, { source_page_id: plan.source_page_id, run_id: plan.run_id });

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href={RECOMMENDATIONS_PATH} className="text-blue-800 underline underline-offset-4">
            ← Recommendations
          </Link>
        </p>
        <h1 className="break-words text-2xl font-semibold">
          Recommendation: link “{plan.anchor_text}”
        </h1>
        <div className="flex flex-wrap items-center gap-4">
          <StatusBadge presentation={status} />
          <span className="text-sm text-slate-700">{status.description}</span>
        </div>
        <p className="text-sm">
          Score: <PlanScore plan={plan} />
        </p>
        <p className="max-w-prose rounded border border-slate-300 bg-slate-50 p-2 text-sm text-slate-800">
          Read-only. Approving, rejecting and editing recommendations arrive in a later phase; this screen changes
          nothing.
        </p>
        {plan.supersedes !== null && (
          <p className="text-sm">
            This plan supersedes an earlier plan:{" "}
            <Link href={planHref(plan.supersedes)} className={`${LINK} font-mono text-xs`}>
              {plan.supersedes}
            </Link>
          </p>
        )}
      </header>

      <Section id="pages" title="Pages">
        <PageFacts plan={plan} />
      </Section>

      <Section id="preview" title="Placement preview">
        <p className="text-sm text-slate-700">
          The anchor text is copied verbatim from the source block; it is highlighted below. Text only — no page HTML
          is rendered.
        </p>
        <PlacementPreviewPanel plan={plan} />
      </Section>

      <Section id="block" title="Source block">
        <SourceBlockText plan={plan} />
        <dl className={FACTS}>
          <Fact term="Block id (hint)">
            <code className="text-xs">{plan.locator.block_id}</code>
          </Fact>
          <Fact term="Block kind">
            <code className="text-xs">{plan.locator.block_kind}</code>
          </Fact>
          <Fact term="Occurrence">
            {plan.locator.occurrence_index + 1} (index {plan.locator.occurrence_index})
          </Fact>
          <Fact term="Offsets">
            <span className="tabular-nums">
              {plan.locator.start}–{plan.locator.end}
            </span>{" "}
            <span className="text-xs text-slate-700">(characters in the normalised block text)</span>
          </Fact>
          <Fact term="Context hash">
            <code className="text-xs" title={plan.locator.context_hash}>
              {shortHash(plan.locator.context_hash)}
            </code>
          </Fact>
          <Fact term="Anchor source">{describePlanAnchorSource(plan.anchor_source)}</Fact>
        </dl>
      </Section>

      <Section id="score" title="Score">
        <ScoreBreakdownTable score={plan.score} />
      </Section>

      <Section id="reasons" title="Why this link">
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
          <Fact term="Found by">
            {plan.channels.length === 0 ? (
              <span className="text-slate-600">—</span>
            ) : (
              plan.channels.map(describeRetrievalChannel).join(", ")
            )}
          </Fact>
        </dl>
        <p>
          <Link href={candidatesHref} className="text-blue-800 underline underline-offset-4">
            Why these targets? Candidates for this source page
          </Link>
        </p>
      </Section>

      <Section id="agent-review" title="Agent review">
        <AgentPlanReviewPanel result={reviewsResult} />
      </Section>

      <Section id="provenance" title="Provenance">
        <dl className={FACTS}>
          <Fact term="Engine version">
            <code className="text-xs">{plan.engine_version}</code>
          </Fact>
          <Fact term="Recommendation policy">version {plan.policy_version}</Fact>
          <Fact term="Ranking policy">version {plan.ranking_policy_version}</Fact>
          <Fact term="Analysis version">
            <code className="text-xs">{plan.analysis_version}</code>
          </Fact>
          <Fact term="Embedding model">
            <Optional value={plan.embedding_model} none="None (no embedding used)" />
          </Fact>
          <Fact term="Reasoning model">
            <Optional value={plan.reasoning_model} none="None (deterministic; no model reasoning)" />
          </Fact>
          <Fact term="Calibration">
            <Optional value={plan.calibration_version} none="None (confidence is raw)" />
          </Fact>
          <Fact term="Source version">v{plan.source_page_version}</Fact>
          <Fact term="Target version">v{plan.target_page_version}</Fact>
          <Fact term="Run">
            {plan.run_id === null ? (
              <span className="text-slate-600">—</span>
            ) : (
              <Link href={jobRunHref(plan.run_id)} className={`${LINK} font-mono text-xs`}>
                {plan.run_id}
              </Link>
            )}
          </Fact>
          <Fact term="Created">
            <time dateTime={plan.created_at}>{formatDateTime(plan.created_at)}</time>
          </Fact>
          <Fact term="Status updated">
            <time dateTime={plan.updated_at}>{formatDateTime(plan.updated_at)}</time>
          </Fact>
          <Fact term="Plan id">
            <code className="text-xs">{plan.id}</code>
          </Fact>
        </dl>
      </Section>

      <Section id="history" title="Status history">
        <TransitionHistory transitions={plan.transitions} />
      </Section>
    </div>
  );
}
