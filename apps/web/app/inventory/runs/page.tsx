import type { Metadata } from "next";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { FilterForm, SelectField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { RunTable } from "@/components/inventory/RunTable";
import { listIngestionRuns } from "@/lib/api";
import {
  DEFAULT_PAGE_SIZE,
  RUN_STATUSES,
  SOURCE_TYPES,
  type SearchParams,
  describeRunStatus,
  describeSourceType,
  parseEnumParam,
  parseLimit,
  parseOffset,
} from "@/lib/inventory";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Ingestion runs",
};

const PATH = "/inventory/runs";

export default async function RunsPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const sourceType = parseEnumParam(params, "source_type", SOURCE_TYPES);
  const status = parseEnumParam(params, "status", RUN_STATUSES);
  const limit = parseLimit(params);
  const offset = parseOffset(params);
  const filtered = sourceType !== undefined || status !== undefined;

  const result = await listIngestionRuns({
    source_type: sourceType ?? null,
    status: status ?? null,
    limit,
    offset,
  });

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Ingestion runs</h1>
        <p className="max-w-prose text-slate-800">
          Crawl and import runs, newest first. Runs are started from the command line, never from
          this console.
        </p>
      </header>

      <FilterForm action={PATH} label="Filter runs" {...(filtered ? { clearHref: PATH } : {})}>
        <SelectField
          id="runs-source-type"
          name="source_type"
          label="Source type"
          value={sourceType}
          options={SOURCE_TYPES.map((value) => ({ value, label: describeSourceType(value) }))}
        />
        <SelectField
          id="runs-status"
          name="status"
          label="Status"
          value={status}
          options={RUN_STATUSES.map((value) => ({ value, label: describeRunStatus(value).label }))}
        />
      </FilterForm>

      <section aria-labelledby="runs-heading" className="space-y-3">
        <h2 id="runs-heading" className="sr-only">
          Runs
        </h2>
        {!result.ok ? (
          <ApiErrorNotice error={result.error} what="Ingestion runs" />
        ) : result.data.page.total === 0 && result.data.items.length === 0 ? (
          <div role="status" className="rounded border border-slate-300 bg-slate-50 p-4 text-slate-800">
            {filtered ? (
              <p>No runs match these filters.</p>
            ) : (
              <p>
                No runs yet — run <code>make ingest-file FILE=…</code> or configure{" "}
                <code>KEN_CRAWL_BASE_URL</code> and run <code>make crawl</code>.
              </p>
            )}
          </div>
        ) : (
          <>
            {result.data.items.length === 0 ? (
              <p role="status" className="text-slate-700">
                No runs on this page of results.
              </p>
            ) : (
              <RunTable runs={result.data.items} total={result.data.page.total} />
            )}
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={PATH}
              params={{
                source_type: sourceType,
                status,
                limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit,
              }}
              label="Runs pagination"
            />
          </>
        )}
      </section>
    </div>
  );
}
