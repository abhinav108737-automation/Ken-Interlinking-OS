import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { FilterForm, SelectField, TextField } from "@/components/inventory/FilterForm";
import { pageHref } from "@/components/inventory/PageInventoryTable";
import { Pagination } from "@/components/inventory/Pagination";
import { CandidateTable } from "@/components/recommendations/CandidateTable";
import { listLinkCandidates } from "@/lib/api";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseEnumParam, parseLimit, parseOffset } from "@/lib/inventory";
import {
  CANDIDATES_PATH,
  OUTCOME_FILTERS,
  RECOMMENDATIONS_PATH,
  outcomeToRejected,
  paramText,
  paramValue,
  parseUuidParam,
} from "@/lib/recommendations";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Recommendation candidates",
};

const OUTCOME_LABEL = { kept: "Kept (became a plan)", rejected: "Rejected by a filter" } as const;

export default async function CandidatesPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const source = parseUuidParam(params, "source_page_id");
  const run = parseUuidParam(params, "run_id");
  const outcome = parseEnumParam(params, "outcome", OUTCOME_FILTERS);
  const limit = parseLimit(params);
  const offset = parseOffset(params);
  const filtered = source.kind !== "absent" || run.kind !== "absent" || outcome !== undefined;
  const invalid = [
    source.kind === "invalid" ? "Source page id must be a page UUID." : null,
    run.kind === "invalid" ? "Run id must be a run UUID." : null,
  ].filter((message): message is string => message !== null);

  const rejected = outcomeToRejected(outcome);
  const result = await listLinkCandidates({
    source_page_id: paramValue(source) ?? null,
    run_id: paramValue(run) ?? null,
    rejected: rejected ?? null,
    limit,
    offset,
  });
  const state = {
    source_page_id: paramValue(source),
    run_id: paramValue(run),
    outcome,
    limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit,
  };
  const sourceId = paramValue(source);

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href={RECOMMENDATIONS_PATH} className="text-blue-800 underline underline-offset-4">
            ← Recommendations
          </Link>
        </p>
        <h1 className="text-2xl font-semibold">Candidates</h1>
        <p className="max-w-prose text-slate-800">
          Every target the engine retrieved, with the channels that found it and either its total score or the first
          deterministic rule that rejected it. Read-only.
        </p>
        {sourceId !== undefined && (
          <p className="text-sm">
            Source page:{" "}
            <Link href={pageHref(sourceId)} className="font-mono text-xs text-blue-800 underline">
              {sourceId}
            </Link>
          </p>
        )}
      </header>

      <FilterForm action={CANDIDATES_PATH} label="Filter candidates" {...(filtered ? { clearHref: CANDIDATES_PATH } : {})}>
        <TextField id="candidates-source" name="source_page_id" label="Source page id" value={paramText(source)} maxLength={80} />
        <TextField id="candidates-run" name="run_id" label="Run id" value={paramText(run)} maxLength={80} />
        <SelectField
          id="candidates-outcome"
          name="outcome"
          label="Outcome"
          value={outcome}
          options={OUTCOME_FILTERS.map((value) => ({ value, label: OUTCOME_LABEL[value] }))}
        />
      </FilterForm>

      {invalid.length > 0 && (
        <div role="alert" className="rounded border border-amber-700 bg-amber-50 p-3 text-sm text-amber-950">
          <p className="font-semibold">
            <span aria-hidden="true">! </span>Some filters were ignored:
          </p>
          <ul className="list-disc pl-5">
            {invalid.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        </div>
      )}

      <section aria-labelledby="candidates-heading" className="space-y-3">
        <h2 id="candidates-heading" className="sr-only">
          Candidates
        </h2>
        {!result.ok ? (
          <ApiErrorNotice error={result.error} what="Candidates" />
        ) : (
          <>
            <CandidateTable
              candidates={result.data.items}
              total={result.data.page.total}
              emptyMessage={
                filtered
                  ? "No candidates match these filters."
                  : "No candidates yet — run make recommend after make analyze."
              }
            />
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={CANDIDATES_PATH}
              params={state}
              label="Candidates pagination"
            />
          </>
        )}
      </section>
    </div>
  );
}
