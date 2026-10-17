import type { Metadata } from "next";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { GraphDefinitions } from "@/components/graph/GraphDefinitions";
import { GraphPageTable } from "@/components/graph/GraphPageTable";
import { FilterForm, SelectField, TextField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { listGraphPages } from "@/lib/api";
import { GRAPH_PAGES_PATH, parseFlag, parseOrdinal } from "@/lib/graph";
import { type SearchParams, parseOffset } from "@/lib/inventory";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "Link graph: pages" };

const PAGE_SIZE = 50;
const FLAG_OPTIONS = [{ value: "1", label: "Only" }];

export default async function GraphPagesPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const cluster = parseOrdinal(params, "cluster_id");
  const component = parseOrdinal(params, "component_id");
  const orphansOnly = parseFlag(params, "orphans_only");
  const bridgesOnly = parseFlag(params, "bridges_only");
  const offset = parseOffset(params);
  const filtered = cluster !== undefined || component !== undefined || orphansOnly || bridgesOnly;
  const result = await listGraphPages({
    cluster_id: cluster ?? null,
    component_id: component ?? null,
    orphans_only: orphansOnly,
    bridges_only: bridgesOnly,
    limit: PAGE_SIZE,
    offset,
  });
  const state = {
    cluster_id: cluster,
    component_id: component,
    orphans_only: orphansOnly ? 1 : undefined,
    bridges_only: bridgesOnly ? 1 : undefined,
  };
  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Link graph: pages</h1>
        <p className="max-w-prose text-slate-800">
          Every page&apos;s position in the internal link graph. The orphan filter doubles as the orphan-pages view.
        </p>
        <GraphDefinitions />
        {orphansOnly && (
          <p role="note" className="max-w-prose rounded border border-slate-400 bg-slate-50 p-3 text-sm text-slate-800">
            The orphan filter returns every page the database flags <code>is_orphan</code>, which includes the crawl
            entry page. It is labelled <strong>Entry page</strong> below and is not a page to recover links to.
          </p>
        )}
      </header>
      <section aria-labelledby="pages-heading" className="space-y-3">
        <h2 id="pages-heading" className="text-xl font-semibold">
          Pages
        </h2>
        <FilterForm
          action={GRAPH_PAGES_PATH}
          label="Filter graph pages"
          {...(filtered ? { clearHref: GRAPH_PAGES_PATH } : {})}
        >
          <TextField id="graph-cluster" name="cluster_id" label="Cluster id" value={cluster?.toString()} maxLength={7} />
          <TextField id="graph-component" name="component_id" label="Component id" value={component?.toString()} maxLength={7} />
          <SelectField id="graph-orphans" name="orphans_only" label="Orphans" value={orphansOnly ? "1" : ""} options={FLAG_OPTIONS} />
          <SelectField id="graph-bridges" name="bridges_only" label="Bridges" value={bridgesOnly ? "1" : ""} options={FLAG_OPTIONS} />
        </FilterForm>
        {result.ok ? (
          <>
            <GraphPageTable rows={result.data.items} total={result.data.page.total} />
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={GRAPH_PAGES_PATH}
              params={state}
              label="Graph pages pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={result.error} what="Graph pages" />
        )}
      </section>
    </div>
  );
}
