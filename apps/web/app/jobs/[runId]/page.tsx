import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { CountTable } from "@/components/intelligence/CountTable";
import { KeyValueTable } from "@/components/inventory/KeyValueTable";
import { runDuration } from "@/components/inventory/RunTable";
import { getJobRun, isMissingResourceError } from "@/lib/api";
import { formatDateTime, toDisplayEntries } from "@/lib/format";
import { describeJobKind, humanizeKey } from "@/lib/intelligence";
import { describeRunStatus } from "@/lib/inventory";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Job run detail",
};

export default async function JobRunDetailPage({ params }: { readonly params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const result = await getJobRun(runId);

  if (!result.ok) {
    if (isMissingResourceError(result.error)) {
      notFound();
    }
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Job run detail</h1>
        <ApiErrorNotice error={result.error} what="Job run" />
        <p>
          <Link href="/jobs" className="text-blue-800 underline underline-offset-4">
            Back to job runs
          </Link>
        </p>
      </div>
    );
  }

  const run = result.data;
  const status = describeRunStatus(run.status);
  const counterRows = Object.entries(run.counters)
    .sort(([a], [b]) => a.localeCompare(b, "en"))
    .map(([key, count]) => ({ key, label: humanizeKey(key), count }));

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href="/jobs" className="text-blue-800 underline underline-offset-4">
            ← Job runs
          </Link>
        </p>
        <h1 className="break-words text-2xl font-semibold">{describeJobKind(run.kind)} run</h1>
        <p className="flex flex-wrap items-center gap-2">
          <StatusBadge presentation={status} />
          <span className="text-sm text-slate-700">{status.description}</span>
        </p>
      </header>

      <section aria-labelledby="job-summary-heading" className="space-y-3">
        <h2 id="job-summary-heading" className="text-xl font-semibold">
          Summary
        </h2>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
          <dt className="font-medium text-slate-700">Kind</dt>
          <dd>
            {describeJobKind(run.kind)} <code className="text-xs text-slate-600">{run.kind}</code>
          </dd>
          <dt className="font-medium text-slate-700">Triggered by</dt>
          <dd className="break-words">{run.triggered_by}</dd>
          <dt className="font-medium text-slate-700">Started</dt>
          <dd>
            <time dateTime={run.started_at}>{formatDateTime(run.started_at)}</time>
          </dd>
          <dt className="font-medium text-slate-700">Completed</dt>
          <dd>{formatDateTime(run.completed_at)}</dd>
          <dt className="font-medium text-slate-700">Duration</dt>
          <dd>{runDuration(run)}</dd>
          <dt className="font-medium text-slate-700">Run id</dt>
          <dd>
            <code className="text-xs">{run.id}</code>
          </dd>
          {run.failure_reason !== null && (
            <>
              <dt className="font-medium text-red-900">Failure reason</dt>
              <dd className="text-red-900">{run.failure_reason}</dd>
            </>
          )}
        </dl>
      </section>

      <section aria-labelledby="job-counters-heading" className="space-y-3">
        <h2 id="job-counters-heading" className="text-xl font-semibold">
          Counters
        </h2>
        <CountTable
          caption="Run counters"
          keyHeader="Counter"
          rows={counterRows}
          emptyMessage="This run recorded no counters."
        />
      </section>

      <section aria-labelledby="job-config-heading" className="space-y-3">
        <h2 id="job-config-heading" className="text-xl font-semibold">
          Configuration snapshot
        </h2>
        <KeyValueTable
          caption="Run configuration"
          keyHeader="Setting"
          rows={toDisplayEntries(run.config_snapshot)}
          emptyMessage="No configuration was recorded for this run."
        />
      </section>
    </div>
  );
}
