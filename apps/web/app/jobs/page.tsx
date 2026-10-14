import type { Metadata } from "next";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { FilterForm, SelectField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { JobRunTable } from "@/components/jobs/JobRunTable";
import { listJobRuns } from "@/lib/api";
import { JOB_KINDS, describeJobKind } from "@/lib/intelligence";
import {
  DEFAULT_PAGE_SIZE,
  RUN_STATUSES,
  type SearchParams,
  describeRunStatus,
  parseEnumParam,
  parseLimit,
  parseOffset,
} from "@/lib/inventory";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Job runs",
};

const PATH = "/jobs";

export default async function JobRunsPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const kind = parseEnumParam(params, "kind", JOB_KINDS);
  const status = parseEnumParam(params, "status", RUN_STATUSES);
  const limit = parseLimit(params);
  const offset = parseOffset(params);
  const filtered = kind !== undefined || status !== undefined;

  const result = await listJobRuns({ kind: kind ?? null, status: status ?? null, limit, offset });

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Job runs</h1>
        <p className="max-w-prose text-slate-800">
          Batch job runs (analysis now; recommendation, verification and others in later phases), newest
          first. Jobs are started from the command line, never from this console. Crawl and import runs are
          listed under Runs.
        </p>
      </header>

      <FilterForm action={PATH} label="Filter job runs" {...(filtered ? { clearHref: PATH } : {})}>
        <SelectField
          id="jobs-kind"
          name="kind"
          label="Kind"
          value={kind}
          options={JOB_KINDS.map((value) => ({ value, label: describeJobKind(value) }))}
        />
        <SelectField
          id="jobs-status"
          name="status"
          label="Status"
          value={status}
          options={RUN_STATUSES.map((value) => ({ value, label: describeRunStatus(value).label }))}
        />
      </FilterForm>

      <section aria-labelledby="jobs-heading" className="space-y-3">
        <h2 id="jobs-heading" className="sr-only">
          Job runs
        </h2>
        {!result.ok ? (
          <ApiErrorNotice error={result.error} what="Job runs" />
        ) : result.data.page.total === 0 && result.data.items.length === 0 ? (
          <div role="status" className="rounded border border-slate-300 bg-slate-50 p-4 text-slate-800">
            {filtered ? (
              <p>No job runs match these filters.</p>
            ) : (
              <p>
                No job runs yet — run <code>make analyze</code> to analyse the inventory.
              </p>
            )}
          </div>
        ) : (
          <>
            {result.data.items.length === 0 ? (
              <p role="status" className="text-slate-700">
                No job runs on this page of results.
              </p>
            ) : (
              <JobRunTable runs={result.data.items} total={result.data.page.total} />
            )}
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={PATH}
              params={{ kind, status, limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit }}
              label="Job runs pagination"
            />
          </>
        )}
      </section>
    </div>
  );
}
