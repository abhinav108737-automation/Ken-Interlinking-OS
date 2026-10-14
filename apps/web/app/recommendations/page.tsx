import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { FilterForm, NumberField, SelectField, TextField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { RecommendationSummaryPanel, totalPlans } from "@/components/recommendations/RecommendationSummaryPanel";
import { RecommendationTable } from "@/components/recommendations/RecommendationTable";
import { getRecommendationSummary, listLinkPlans } from "@/lib/api";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseLimit, parseOffset } from "@/lib/inventory";
import {
  ALL_STATUSES,
  CANDIDATES_PATH,
  DEFAULT_STATUS_FILTER,
  LINK_PLAN_STATUSES,
  OPPORTUNITIES_PATH,
  RECOMMENDATIONS_PATH,
  describeLinkPlanStatus,
  paramText,
  paramValue,
  parseMinScore,
  parseStatusFilter,
  parseUuidParam,
} from "@/lib/recommendations";

// Plans are live data: never prerender or cache this page.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Recommendations",
};

const STATUS_OPTIONS = [
  { value: ALL_STATUSES, label: "Any status" },
  ...LINK_PLAN_STATUSES.map((value) => ({ value, label: describeLinkPlanStatus(value).label })),
];

export default async function RecommendationsPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const status = parseStatusFilter(params);
  const minScore = parseMinScore(params);
  const source = parseUuidParam(params, "source_page_id");
  const target = parseUuidParam(params, "target_page_id");
  const limit = parseLimit(params);
  const offset = parseOffset(params);
  const filtered =
    status !== DEFAULT_STATUS_FILTER || minScore.kind !== "absent" || source.kind !== "absent" || target.kind !== "absent";
  const invalid = [
    minScore.kind === "invalid" ? "Minimum score must be a number from 0 to 1." : null,
    source.kind === "invalid" ? "Source page id must be a page UUID." : null,
    target.kind === "invalid" ? "Target page id must be a page UUID." : null,
  ].filter((message): message is string => message !== null);

  const [result, summaryResult] = await Promise.all([
    listLinkPlans({
      status: status === ALL_STATUSES ? null : status,
      min_score: paramValue(minScore) ?? null,
      source_page_id: paramValue(source) ?? null,
      target_page_id: paramValue(target) ?? null,
      limit,
      offset,
    }),
    getRecommendationSummary(),
  ]);

  // "Nothing yet" only when the engine has produced no plan at all; otherwise the filters exclude them.
  const noPlansAtAll = summaryResult.ok ? totalPlans(summaryResult.data) === 0 : !filtered;
  const state = {
    status: status === DEFAULT_STATUS_FILTER ? undefined : status,
    min_score: paramValue(minScore) === undefined ? undefined : paramText(minScore),
    source_page_id: paramValue(source),
    target_page_id: paramValue(target),
    limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit,
  };

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Recommendations</h1>
        <p className="max-w-prose text-slate-800">
          Proposed contextual links from the deterministic recommendation engine, highest score first. This view is
          read-only: nothing here approves, rejects, edits or publishes a link. Runs start from the command line (
          <code>make recommend</code>) and are listed under{" "}
          <Link href="/jobs" className="text-blue-800 underline underline-offset-4">
            Jobs
          </Link>
          .
        </p>
        <p className="flex flex-wrap gap-4">
          <Link href={OPPORTUNITIES_PATH} className="text-blue-800 underline underline-offset-4">
            Linking opportunities
          </Link>
          <Link href={CANDIDATES_PATH} className="text-blue-800 underline underline-offset-4">
            Candidates (kept and rejected)
          </Link>
        </p>
      </header>

      <section aria-labelledby="summary-heading" className="space-y-3">
        <h2 id="summary-heading" className="text-xl font-semibold">
          Overview
        </h2>
        {summaryResult.ok ? (
          <RecommendationSummaryPanel summary={summaryResult.data} />
        ) : (
          <ApiErrorNotice error={summaryResult.error} what="Recommendation summary" />
        )}
      </section>

      <section aria-labelledby="plans-heading" className="space-y-3">
        <h2 id="plans-heading" className="text-xl font-semibold">
          Link plans
        </h2>
        <FilterForm action={RECOMMENDATIONS_PATH} label="Filter recommendations" {...(filtered ? { clearHref: RECOMMENDATIONS_PATH } : {})}>
          <SelectField
            id="plans-status"
            name="status"
            label="Status"
            value={status}
            options={STATUS_OPTIONS}
            includeAny={false}
          />
          <NumberField
            id="plans-min-score"
            name="min_score"
            label="Minimum score"
            value={paramText(minScore)}
            min={0}
            max={1}
            step={0.01}
            describedBy="plans-min-score-hint"
          />
          <TextField id="plans-source" name="source_page_id" label="Source page id" value={paramText(source)} maxLength={80} />
          <TextField id="plans-target" name="target_page_id" label="Target page id" value={paramText(target)} maxLength={80} />
          <p id="plans-min-score-hint" className="w-full text-xs text-slate-700">
            Minimum score is the total score from 0 to 1. Page ids are UUIDs, as shown on a page&apos;s detail screen.
          </p>
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

        {!result.ok ? (
          <ApiErrorNotice error={result.error} what="Recommendations" />
        ) : result.data.page.total === 0 && result.data.items.length === 0 ? (
          <div role="status" className="rounded border border-slate-300 bg-slate-50 p-4 text-slate-800">
            {noPlansAtAll ? (
              <p>
                No recommendations yet — run <code>make recommend</code> after <code>make analyze</code>.
              </p>
            ) : (
              <p>
                No recommendations match these filters
                {status === DEFAULT_STATUS_FILTER ? " (only proposed plans are shown by default)" : ""}.
              </p>
            )}
          </div>
        ) : (
          <>
            {result.data.items.length === 0 ? (
              <p role="status" className="text-slate-700">
                No recommendations on this page of results.
              </p>
            ) : (
              <RecommendationTable
                plans={result.data.items}
                total={result.data.page.total}
                caption={
                  status === ALL_STATUSES
                    ? "Recommendations (any status)"
                    : `Recommendations: ${describeLinkPlanStatus(status).label.toLowerCase()}`
                }
              />
            )}
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={RECOMMENDATIONS_PATH}
              params={state}
              label="Recommendations pagination"
            />
          </>
        )}
      </section>
    </div>
  );
}
