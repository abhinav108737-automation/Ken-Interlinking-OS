import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { FilterForm, SelectField } from "@/components/inventory/FilterForm";
import { IssueTable } from "@/components/inventory/IssueTable";
import { KeyValueTable } from "@/components/inventory/KeyValueTable";
import { Pagination } from "@/components/inventory/Pagination";
import { RunCountersList, runDuration, runHref } from "@/components/inventory/RunTable";
import { getIngestionRun, isNotFoundError, listIngestionRunIssues } from "@/lib/api";
import { formatCount, formatDateTime, toDisplayEntries } from "@/lib/format";
import {
  DEFAULT_PAGE_SIZE,
  ISSUE_SEVERITIES,
  type SearchParams,
  describeIssueSeverity,
  describeRunStatus,
  describeSourceType,
  parseEnumParam,
  parseOffset,
  sortIssueCounts,
} from "@/lib/inventory";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Run detail",
};

export default async function RunDetailPage({
  params,
  searchParams,
}: {
  readonly params: Promise<{ runId: string }>;
  readonly searchParams: Promise<SearchParams>;
}) {
  const [{ runId }, query] = await Promise.all([params, searchParams]);
  const severity = parseEnumParam(query, "severity", ISSUE_SEVERITIES);
  const offset = parseOffset(query);

  const [runResult, issuesResult] = await Promise.all([
    getIngestionRun(runId),
    listIngestionRunIssues(runId, { severity: severity ?? null, limit: DEFAULT_PAGE_SIZE, offset }),
  ]);

  if (!runResult.ok) {
    const error = runResult.error;
    // Missing, or an id the API/client rejects as malformed: both mean "no such run".
    const missing =
      isNotFoundError(error) ||
      error.kind === "invalid_argument" ||
      (error.kind === "api_error" && error.status === 422);
    if (missing) {
      notFound();
    }
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Run detail</h1>
        <ApiErrorNotice error={error} what="Ingestion run" />
        <p>
          <Link href="/inventory/runs" className="text-blue-800 underline underline-offset-4">
            Back to ingestion runs
          </Link>
        </p>
      </div>
    );
  }

  const run = runResult.data;
  const self = runHref(run.id);
  const issueCounts = sortIssueCounts(run.issue_counts);
  const configRows = toDisplayEntries(run.config_snapshot);

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href="/inventory/runs" className="text-blue-800 underline underline-offset-4">
            ← Ingestion runs
          </Link>
        </p>
        <h1 className="break-words text-2xl font-semibold">Run: {run.source_name}</h1>
        <p className="flex flex-wrap items-center gap-2">
          <StatusBadge presentation={describeRunStatus(run.status)} />
          <span className="text-sm text-slate-700">{describeRunStatus(run.status).description}</span>
        </p>
      </header>

      <section aria-labelledby="run-facts-heading" className="space-y-3">
        <h2 id="run-facts-heading" className="text-xl font-semibold">
          Summary
        </h2>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
          <dt className="font-medium text-slate-700">Source</dt>
          <dd>
            {describeSourceType(run.source_type)} · <span className="break-all">{run.source_name}</span>
          </dd>
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

      <section aria-labelledby="counters-heading" className="space-y-3">
        <h2 id="counters-heading" className="text-xl font-semibold">
          Counters
        </h2>
        <RunCountersList counters={run.counters} />
      </section>

      <section aria-labelledby="config-heading" className="space-y-3">
        <h2 id="config-heading" className="text-xl font-semibold">
          Configuration snapshot
        </h2>
        <KeyValueTable
          caption="Run configuration"
          keyHeader="Setting"
          rows={configRows}
          emptyMessage="No configuration was recorded for this run."
        />
      </section>

      <section aria-labelledby="issue-counts-heading" className="space-y-3">
        <h2 id="issue-counts-heading" className="text-xl font-semibold">
          Issues by code
        </h2>
        <KeyValueTable
          caption="Issue counts"
          keyHeader="Code"
          valueHeader="Count"
          rows={issueCounts.map(({ code, count }) => ({ key: code, value: formatCount(count) }))}
          emptyMessage="This run recorded no issues."
        />
      </section>

      <section aria-labelledby="issues-heading" className="space-y-3">
        <h2 id="issues-heading" className="text-xl font-semibold">
          Issues
        </h2>
        <FilterForm action={self} label="Filter issues" {...(severity !== undefined ? { clearHref: self } : {})}>
          <SelectField
            id="issues-severity"
            name="severity"
            label="Severity"
            value={severity}
            options={ISSUE_SEVERITIES.map((value) => ({ value, label: describeIssueSeverity(value).label }))}
          />
        </FilterForm>
        {issuesResult.ok ? (
          <>
            <IssueTable
              issues={issuesResult.data.items}
              total={issuesResult.data.page.total}
              emptyMessage={severity === undefined ? "This run recorded no issues." : "No issues of this severity."}
            />
            <Pagination
              page={issuesResult.data.page}
              itemCount={issuesResult.data.items.length}
              pathname={self}
              params={{ severity }}
              label="Issues pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={issuesResult.error} what="Run issues" />
        )}
      </section>

    </div>
  );
}
