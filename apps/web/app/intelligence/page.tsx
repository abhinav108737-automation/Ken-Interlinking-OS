import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { CountTable } from "@/components/intelligence/CountTable";
import { CoverageSummary } from "@/components/intelligence/CoverageSummary";
import { PageTypeTable, TaxonomyTree } from "@/components/intelligence/TaxonomyTree";
import { type TaxonomyView, getIntelligenceCoverage, getTaxonomy } from "@/lib/api";
import { nodeLabel, sortCounts, taxonomyLabels, taxonomyNodeHref } from "@/lib/intelligence";

// Coverage is live data: never prerender or cache this page.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Content intelligence",
};

function TaxonomyDetails({ taxonomy }: { readonly taxonomy: TaxonomyView }) {
  return (
    <>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
        <dt className="font-medium text-slate-700">Version</dt>
        <dd>{taxonomy.version}</dd>
        <dt className="font-medium text-slate-700">Status</dt>
        <dd>{taxonomy.status}</dd>
        <dt className="font-medium text-slate-700">Source</dt>
        <dd>
          {taxonomy.source}
          {taxonomy.source === "fixture" && (
            <span className="text-slate-700"> (illustrative seed, not an official taxonomy)</span>
          )}
        </dd>
        <dt className="font-medium text-slate-700">Analysis version</dt>
        <dd>
          <code className="text-xs">{taxonomy.analysis_version}</code>
        </dd>
      </dl>
      <p className="text-sm text-slate-700">
        Page counts include only pages whose fresh analysis lists the node.
      </p>

      <section aria-labelledby="markets-heading" className="space-y-2">
        <h3 id="markets-heading" className="text-lg font-semibold">
          Markets
        </h3>
        <TaxonomyTree nodes={taxonomy.markets} label="Markets" emptyMessage="The taxonomy defines no markets." />
      </section>

      <section aria-labelledby="geographies-heading" className="space-y-2">
        <h3 id="geographies-heading" className="text-lg font-semibold">
          Geographies
        </h3>
        <TaxonomyTree
          nodes={taxonomy.geographies}
          label="Geographies"
          showKind
          emptyMessage="The taxonomy defines no geographies."
        />
      </section>

      <section aria-labelledby="page-types-heading" className="space-y-2">
        <h3 id="page-types-heading" className="text-lg font-semibold">
          Page types
        </h3>
        <PageTypeTable pageTypes={taxonomy.page_types} />
      </section>
    </>
  );
}

export default async function IntelligencePage() {
  const [coverageResult, taxonomyResult] = await Promise.all([getIntelligenceCoverage(), getTaxonomy()]);
  const taxonomy = taxonomyResult.ok ? taxonomyResult.data : null;
  const labels = taxonomyLabels(taxonomy);
  const pageTypeLabels = new Map((taxonomy?.page_types ?? []).map((type) => [type.id, type.label]));

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Content intelligence</h1>
        <p className="max-w-prose text-slate-800">
          How much of the inventory is analysed, embedded, protected and eligible for linking, and the
          taxonomy the deterministic analysis classifies pages into. This view is read-only; analysis runs
          start from the command line (<code>make analyze</code>) and are listed under{" "}
          <Link href="/jobs" className="text-blue-800 underline underline-offset-4">
            Jobs
          </Link>
          .
        </p>
        <p>
          <Link href="/intelligence/search" className="text-blue-800 underline underline-offset-4">
            Semantic search
          </Link>
        </p>
      </header>

      <section aria-labelledby="coverage-heading" className="space-y-4">
        <h2 id="coverage-heading" className="text-xl font-semibold">
          Coverage
        </h2>
        {coverageResult.ok ? (
          <>
            <CoverageSummary coverage={coverageResult.data} />
            <div className="grid gap-4 lg:grid-cols-2">
              <CountTable
                caption="Pages by page type"
                keyHeader="Page type"
                valueHeader="Pages"
                rows={sortCounts(coverageResult.data.by_page_type).map(({ key, count }) => ({
                  key,
                  label: pageTypeLabels.get(key) ?? key,
                  count,
                }))}
                emptyMessage="No analysed pages yet."
              />
              <CountTable
                caption="Pages by industry"
                keyHeader="Industry"
                valueHeader="Pages"
                rows={sortCounts(coverageResult.data.by_industry).map(({ key, count }) => ({
                  key,
                  label: nodeLabel(labels, key),
                  count,
                  href: taxonomyNodeHref(key),
                }))}
                emptyMessage="No analysed pages were classified into an industry yet."
              />
            </div>
          </>
        ) : (
          <ApiErrorNotice error={coverageResult.error} what="Intelligence coverage" />
        )}
      </section>

      <section aria-labelledby="taxonomy-heading" className="space-y-4">
        <h2 id="taxonomy-heading" className="text-xl font-semibold">
          Taxonomy
        </h2>
        {taxonomyResult.ok ? (
          <TaxonomyDetails taxonomy={taxonomyResult.data} />
        ) : (
          <ApiErrorNotice error={taxonomyResult.error} what="Taxonomy" />
        )}
      </section>
    </div>
  );
}
