import type { Metadata } from "next";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { FilterForm, SelectField, TextField } from "@/components/inventory/FilterForm";
import { PageInventoryTable } from "@/components/inventory/PageInventoryTable";
import { Pagination } from "@/components/inventory/Pagination";
import { listInventoryPages } from "@/lib/api";
import {
  DEFAULT_PAGE_SIZE,
  MAX_QUERY_LENGTH,
  PUBLICATION_STATES,
  type SearchParams,
  describePublicationState,
  parseEnumParam,
  parseLimit,
  parseOffset,
  parseSearchQuery,
} from "@/lib/inventory";

// Inventory is live data: never prerender or cache this page.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Content inventory",
};

const PATH = "/inventory";

export default async function InventoryPage({
  searchParams,
}: {
  readonly searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const q = parseSearchQuery(params);
  const publicationState = parseEnumParam(params, "publication_state", PUBLICATION_STATES);
  const limit = parseLimit(params);
  const offset = parseOffset(params);
  const filtered = q !== undefined || publicationState !== undefined;

  const result = await listInventoryPages({
    q: q ?? null,
    publication_state: publicationState ?? null,
    limit,
    offset,
  });

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Content inventory</h1>
        <p className="max-w-prose text-slate-800">
          Every page taken stock of by a crawl or import, described by its current version. This
          view is read-only.
        </p>
      </header>

      <FilterForm action={PATH} label="Filter pages" {...(filtered ? { clearHref: PATH } : {})}>
        <TextField
          id="inventory-q"
          name="q"
          label="Search title or URL"
          value={q}
          maxLength={MAX_QUERY_LENGTH}
        />
        <SelectField
          id="inventory-publication-state"
          name="publication_state"
          label="Publication state"
          value={publicationState}
          options={PUBLICATION_STATES.map((value) => ({
            value,
            label: describePublicationState(value).label,
          }))}
        />
      </FilterForm>

      <section aria-labelledby="pages-heading" className="space-y-3">
        <h2 id="pages-heading" className="sr-only">
          Pages
        </h2>
        {!result.ok ? (
          <ApiErrorNotice error={result.error} what="Page inventory" />
        ) : result.data.items.length === 0 && result.data.page.total === 0 ? (
          <div role="status" className="rounded border border-slate-300 bg-slate-50 p-4 text-slate-800">
            {filtered ? (
              <p>No pages match these filters.</p>
            ) : (
              <p>
                No pages yet — run <code>make ingest-file FILE=…</code> or configure{" "}
                <code>KEN_CRAWL_BASE_URL</code> and run <code>make crawl</code>.
              </p>
            )}
          </div>
        ) : (
          <>
            {result.data.items.length === 0 ? (
              <p role="status" className="text-slate-700">
                No pages on this page of results.
              </p>
            ) : (
              <PageInventoryTable pages={result.data.items} total={result.data.page.total} />
            )}
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={PATH}
              params={{
                q,
                publication_state: publicationState,
                limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit,
              }}
              label="Pages pagination"
            />
          </>
        )}
      </section>
    </div>
  );
}
