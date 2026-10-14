import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { AgentReviewTable } from "@/components/agents/AgentReviewTable";
import { AgentStepTimeline } from "@/components/agents/AgentStepTimeline";
import { KeyValueTable } from "@/components/inventory/KeyValueTable";
import { pageHref } from "@/components/inventory/PageInventoryTable";
import {
  AGENTS_PATH,
  agentRunDuration,
  describeAgentErrorCode,
  describeAgentOutcome,
  describeAgentRunStatus,
  describeAgentTrigger,
  describeTriageDecision,
} from "@/lib/agents";
import { getAgentRun, isMissingResourceError } from "@/lib/api";
import { formatDateTime, toDisplayEntries } from "@/lib/format";
import { jobRunHref } from "@/lib/intelligence";
import { planHref } from "@/lib/recommendations";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Agent run detail",
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

export default async function AgentRunDetailPage({ params }: { readonly params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const result = await getAgentRun(runId);

  if (!result.ok) {
    if (isMissingResourceError(result.error)) {
      notFound();
    }
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Agent run detail</h1>
        <ApiErrorNotice error={result.error} what="Agent run" />
        <p>
          <Link href={AGENTS_PATH} className="text-blue-800 underline underline-offset-4">
            Back to agent runs
          </Link>
        </p>
      </div>
    );
  }

  const run = result.data;
  const status = describeAgentRunStatus(run.status);
  const outcome = describeAgentOutcome(run.outcome);

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href={AGENTS_PATH} className="text-blue-800 underline underline-offset-4">
            ← Agent runs
          </Link>
        </p>
        <h1 className="break-words text-2xl font-semibold">Agent run</h1>
        <p className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-slate-700">Status</span>
          <StatusBadge presentation={status} />
          <span className="text-sm text-slate-700">{status.description}</span>
        </p>
        <p className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-slate-700">Outcome</span>
          <StatusBadge presentation={outcome} />
          <span className="text-sm text-slate-700">{outcome.description}</span>
        </p>
        {run.error_code !== null && (
          <p className="text-sm text-red-900">
            <span className="font-medium">Error:</span> {describeAgentErrorCode(run.error_code)}{" "}
            <code className="text-xs">{run.error_code}</code>
          </p>
        )}
        <p className="max-w-prose rounded border border-slate-300 bg-slate-50 p-2 text-sm text-slate-800">
          Read-only. An agent run only reviews proposed plans; a person approves or rejects every handed-off plan in a
          later phase. This screen changes nothing.
        </p>
      </header>

      <Section id="run-summary" title="Summary">
        <dl className={FACTS}>
          <Fact term="Source page">
            <Link href={pageHref(run.source_page_id)} className={`${LINK} break-all font-mono text-xs`}>
              {run.source_url}
            </Link>
            <span className="block text-xs text-slate-600">Opens the page&apos;s inventory record.</span>
          </Fact>
          <Fact term="Source version">
            {run.source_page_version === null ? (
              <span className="text-slate-600">— (not loaded)</span>
            ) : (
              `v${run.source_page_version}`
            )}
          </Fact>
          <Fact term="Trigger">{describeAgentTrigger(run.trigger)}</Fact>
          <Fact term="Triage">
            {run.triage_decision === null ? (
              <span className="text-slate-600">— (not reached)</span>
            ) : (
              <>
                {describeTriageDecision(run.triage_decision)}{" "}
                <code className="text-xs text-slate-600">{run.triage_decision}</code>
              </>
            )}
          </Fact>
          <Fact term="Started">
            <time dateTime={run.started_at}>{formatDateTime(run.started_at)}</time>
          </Fact>
          <Fact term="Finished">{formatDateTime(run.finished_at)}</Fact>
          <Fact term="Duration">{agentRunDuration(run)}</Fact>
          <Fact term="Graph version">
            <code className="text-xs">{run.graph_version}</code>
          </Fact>
          <Fact term="Correlation id">
            <code className="text-xs">{run.correlation_id}</code>
          </Fact>
          <Fact term="Trace id">
            <code className="text-xs">{run.trace_id}</code>
          </Fact>
          <Fact term="Agent batch">
            <Link href={jobRunHref(run.job_run_id)} className={`${LINK} font-mono text-xs`}>
              {run.job_run_id}
            </Link>
          </Fact>
          <Fact term="Run id">
            <code className="text-xs">{run.id}</code>
          </Fact>
          <Fact term="Handed off">
            {run.handed_off_plan_ids.length === 0 ? (
              <span className="text-slate-600">No plans</span>
            ) : (
              <ul aria-label="Handed-off plans" className="space-y-0.5">
                {run.handed_off_plan_ids.map((planId) => (
                  <li key={planId}>
                    <Link href={planHref(planId)} className={`${LINK} font-mono text-xs`}>
                      {planId}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Fact>
        </dl>
      </Section>

      <Section id="review-notes" title="Review notes">
        {run.review_notes.length === 0 ? (
          <p className="text-slate-700">No review notes.</p>
        ) : (
          <ul aria-label="Review notes" className="list-disc space-y-1 pl-5">
            {run.review_notes.map((note, index) => (
              <li key={index} className="whitespace-pre-wrap break-words">
                {note}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section id="reviews" title="Plan reviews">
        <p className="max-w-prose text-sm text-slate-700">
          Explanations are model output and are shown as plain text. A rejected plan was not handed off and stays
          proposed; only a person can reject a plan.
        </p>
        <AgentReviewTable
          reviews={run.reviews}
          caption="Plan reviews"
          emptyMessage="This run reviewed no plans."
        />
      </Section>

      <Section id="steps" title="Steps">
        <AgentStepTimeline steps={run.steps} />
      </Section>

      <Section id="config" title="Configuration snapshot">
        <KeyValueTable
          caption="Run configuration"
          keyHeader="Setting"
          rows={toDisplayEntries(run.config_snapshot)}
          emptyMessage="No configuration was recorded for this run."
        />
      </Section>
    </div>
  );
}
