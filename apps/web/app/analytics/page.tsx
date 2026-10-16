import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { DailyTotalsChart } from "@/components/analytics/DailyTotalsChart";
import { ImportsTable } from "@/components/analytics/ImportsTable";
import { KpiTiles } from "@/components/analytics/KpiTiles";
import { LinkPerformanceTable } from "@/components/analytics/LinkPerformanceTable";
import { NoAnalyticsData } from "@/components/analytics/NoAnalyticsData";
import { RangeForm } from "@/components/analytics/RangeForm";
import { SourceFreshnessTable } from "@/components/analytics/SourceFreshnessTable";
import { SyntheticBanner } from "@/components/analytics/SyntheticNotice";
import { getAnalyticsSummary, listAnalyticsImports, listLinkPerformance } from "@/lib/api";
import {
  ANALYTICS_PATH,
  FIRST_PARTY_CAVEAT,
  LINK_SOURCES,
  SOURCE_SUM_CAVEAT,
  describeRange,
  describeSource,
  foldDailyTotals,
  hasSynthetic,
  parseRange,
  rangeQuery,
  sourcesOf,
  totalsFor,
} from "@/lib/analytics";
import { type SearchParams, parseEnumParam } from "@/lib/inventory";

// Analytics is live data: never prerender or cache this page.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Analytics",
};

const TOP_LINKS_LIMIT = 10;
const IMPORTS_LIMIT = 10;

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
    <section aria-labelledby={`${id}-heading`} className="space-y-3">
      <h2 id={`${id}-heading`} className="text-xl font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

export default async function AnalyticsPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const range = parseRange(params);
  const source = parseEnumParam(params, "source", LINK_SOURCES);
  const query = rangeQuery(range);
  const filtered = source !== undefined || range.kind !== "absent";

  // The summary covers every source; a chosen source also narrows the top-links
  // table, which is the only route that takes a `source` filter.
  const [summaryResult, importsResult, sourceLinksResult] = await Promise.all([
    getAnalyticsSummary(query),
    listAnalyticsImports({ limit: IMPORTS_LIMIT }),
    source === undefined
      ? Promise.resolve(null)
      : listLinkPerformance({ ...query, source, sort: "clicks", order: "desc", limit: TOP_LINKS_LIMIT }),
  ]);

  const summary = summaryResult.ok ? summaryResult.data : null;
  const imports = importsResult.ok ? importsResult.data.items : [];
  const topLinks =
    sourceLinksResult === null
      ? (summary?.top_links ?? [])
      : sourceLinksResult.ok
        ? sourceLinksResult.data.items
        : [];
  const scopeLabel = source === undefined ? "all sources" : describeSource(source);
  const synthetic = hasSynthetic(
    summary?.synthetic,
    imports.some((row) => row.synthetic),
    topLinks.some((link) => link.synthetic === true),
  );
  const storedSources = summary === null ? [] : summary.sources.filter((row) => row.latest_date !== null);
  const noData = summary !== null && storedSources.length === 0 && summary.daily.length === 0;
  const totalSources = summary === null ? [] : sourcesOf(summary.totals_by_source);

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Analytics</h1>
        <p className="max-w-prose text-slate-800">
          What happened to the links this system deployed: impressions and clicks per link and per day, which sources
          hold data, and when each was last imported. Everything here is read-only and comes from stored rows. Imports
          and aggregation run from the command line only — this console never calls an analytics provider.
        </p>
      </header>

      {synthetic && <SyntheticBanner />}

      <RangeForm range={range} source={source} {...(filtered ? { clearHref: ANALYTICS_PATH } : {})} />

      {range.kind === "invalid" && (
        <p role="alert" className="rounded border border-amber-700 bg-amber-50 p-3 text-sm text-amber-950">
          <span aria-hidden="true">! </span>
          The date range was ignored: {range.message} The latest range the API holds is shown instead.
        </p>
      )}

      {!summaryResult.ok ? (
        <ApiErrorNotice error={summaryResult.error} what="Analytics summary" />
      ) : noData ? (
        <NoAnalyticsData />
      ) : (
        summary !== null && (
          <>
            <Section id="totals" title="Totals">
              <p className="text-sm text-slate-700">Range shown: {describeRange(summary.range)}.</p>
              <KpiTiles
                liveLinks={summary.live_links}
                linksWithData={summary.links_with_data}
                totals={totalsFor(summary.totals_by_source, source)}
                scopeLabel={scopeLabel}
              />
              {source === undefined && totalSources.length > 1 && (
                <p className="max-w-prose text-xs text-slate-700">{SOURCE_SUM_CAVEAT}</p>
              )}
              {totalSources.includes("first_party") && (
                <p className="max-w-prose text-xs text-slate-700">{FIRST_PARTY_CAVEAT}</p>
              )}
            </Section>

            <Section id="daily" title="By day">
              <DailyTotalsChart
                id="analytics-daily"
                points={foldDailyTotals(summary.daily, source)}
                scopeLabel={scopeLabel}
                caption="Link impressions and clicks per day"
              />
            </Section>

            <Section id="top-links" title="Top links">
              {sourceLinksResult !== null && !sourceLinksResult.ok ? (
                <ApiErrorNotice error={sourceLinksResult.error} what="Link performance" />
              ) : (
                <LinkPerformanceTable
                  links={topLinks}
                  caption={`Most clicked links (${scopeLabel})`}
                  emptyMessage="No link has a stored row in this range."
                />
              )}
            </Section>
          </>
        )
      )}

      {summaryResult.ok && (
        <Section id="freshness" title="Source freshness">
          <SourceFreshnessTable sources={summaryResult.data.sources} />
        </Section>
      )}

      <Section id="imports" title="Recent imports">
        {!importsResult.ok ? (
          <ApiErrorNotice error={importsResult.error} what="Imports" />
        ) : (
          <>
            <ImportsTable imports={imports} total={importsResult.data.page.total} />
            <p className="text-sm text-slate-700">
              A failed import leaves no ledger row; look for it in{" "}
              <Link href="/jobs" className="text-blue-800 underline underline-offset-4">
                job runs
              </Link>
              .
            </p>
          </>
        )}
      </Section>
    </div>
  );
}
