import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { ObservedChangeNote } from "@/components/analytics/ObservedChangeNote";
import { PageSeriesPanel } from "@/components/analytics/PageSeriesPanel";
import { SyntheticBanner } from "@/components/analytics/SyntheticNotice";
import { WindowComparisonCard } from "@/components/analytics/WindowComparisonCard";
import { FilterForm, SelectField } from "@/components/inventory/FilterForm";
import { ExternalUrl } from "@/components/inventory/ExternalUrl";
import { pageHref } from "@/components/inventory/PageInventoryTable";
import { getPageImpact, isMissingResourceError } from "@/lib/api";
import {
  ANALYTICS_PATH,
  WINDOW_DAY_CHOICES,
  describeSource,
  pageImpactHref,
  parseWindowDays,
  sourcesOf,
} from "@/lib/analytics";
import type { SearchParams } from "@/lib/inventory";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Page impact",
};

export default async function PageImpactPage({
  params,
  searchParams,
}: {
  readonly params: Promise<{ pageId: string }>;
  readonly searchParams?: Promise<SearchParams>;
}) {
  const { pageId } = await params;
  const query = (await searchParams) ?? {};
  const windowDays = parseWindowDays(query);
  const result = await getPageImpact(pageId, { window_days: windowDays });

  if (!result.ok) {
    if (isMissingResourceError(result.error)) {
      notFound();
    }
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Page impact</h1>
        <ApiErrorNotice error={result.error} what="Page impact" />
        <p>
          <Link href={ANALYTICS_PATH} className="text-blue-800 underline underline-offset-4">
            Back to analytics
          </Link>
        </p>
      </div>
    );
  }

  const view = result.data;
  const self = pageImpactHref(view.page_id);
  const seriesSources = sourcesOf(view.series);

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href={ANALYTICS_PATH} className="text-blue-800 underline underline-offset-4">
            ← Analytics
          </Link>
        </p>
        <h1 className="break-words text-2xl font-semibold">Page impact</h1>
        <p className="break-words">
          <Link href={pageHref(view.page_id)} className="text-blue-800 underline underline-offset-4">
            View this page in the inventory
          </Link>{" "}
          · <ExternalUrl url={view.url} />
        </p>
        <p className="text-sm text-slate-700">
          Windows of {view.window_days} days before and after each link change on this page.
        </p>
      </header>

      {view.synthetic === true && <SyntheticBanner />}

      <ObservedChangeNote />

      <FilterForm action={self} label="Choose the comparison window" submitLabel="Apply window">
        <SelectField
          id="page-impact-window"
          name="window_days"
          label="Window (days)"
          value={String(view.window_days)}
          includeAny={false}
          options={WINDOW_DAY_CHOICES.map((days) => ({ value: String(days), label: `${days} days` }))}
        />
      </FilterForm>

      <section aria-labelledby="page-changes-heading" className="space-y-4">
        <h2 id="page-changes-heading" className="text-xl font-semibold">
          Link changes
        </h2>
        {view.changes.length === 0 ? (
          <p role="status" className="rounded border border-slate-300 bg-slate-50 p-3 text-slate-800">
            No link has been deployed on or to this page, so there is nothing to compare.
          </p>
        ) : (
          view.changes.map((comparison, index) => (
            <WindowComparisonCard
              key={`${comparison.plan_id}:${comparison.source}:${comparison.role}:${index}`}
              id={`page-change-${index}`}
              comparison={comparison}
            />
          ))
        )}
      </section>

      <section aria-labelledby="page-series-heading" className="space-y-4">
        <h2 id="page-series-heading" className="text-xl font-semibold">
          Daily rows
        </h2>
        {seriesSources.length === 0 ? (
          <p role="status" className="rounded border border-slate-300 bg-slate-50 p-3 text-slate-800">
            No page-level data is stored for this page yet.
          </p>
        ) : (
          seriesSources.map((source) => (
            <section key={source} aria-labelledby={`page-series-${source}-heading`} className="space-y-2">
              <h3 id={`page-series-${source}-heading`} className="font-semibold">
                {describeSource(source)}
              </h3>
              <PageSeriesPanel id={`page-series-${source}`} source={source} points={view.series[source] ?? []} />
            </section>
          ))
        )}
      </section>
    </div>
  );
}
