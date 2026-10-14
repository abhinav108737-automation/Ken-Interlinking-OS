import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { ModelUnavailableNotice } from "@/components/intelligence/ModelUnavailableNotice";
import { SimilarPageTable } from "@/components/intelligence/SimilarPageTable";
import { FilterForm, SelectField, TextField } from "@/components/inventory/FilterForm";
import { getTaxonomy, isModelUnavailableError, semanticSearch } from "@/lib/api";
import {
  SEARCH_DEFAULT_LIMIT,
  SEARCH_LIMITS,
  SEARCH_MAX_LENGTH,
  SEARCH_MIN_LENGTH,
  parseSearchLimit,
  parseSemanticQuery,
  taxonomyLabels,
} from "@/lib/intelligence";
import type { SearchParams } from "@/lib/inventory";

// Every search embeds the query at request time: never prerender or cache.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Semantic search",
};

const PATH = "/intelligence/search";

export default async function SemanticSearchPage({
  searchParams,
}: {
  readonly searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const query = parseSemanticQuery(params);
  const limit = parseSearchLimit(params);

  // No request is made for an empty or invalid query.
  const [searchResult, taxonomyResult] =
    query.kind === "ok"
      ? await Promise.all([semanticSearch({ q: query.q, limit }), getTaxonomy()])
      : [null, null];
  const labels = taxonomyLabels(taxonomyResult?.ok ? taxonomyResult.data : null);
  const shownQuery = query.kind === "empty" ? undefined : query.q;

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href="/intelligence" className="text-blue-800 underline underline-offset-4">
            ← Content intelligence
          </Link>
        </p>
        <h1 className="text-2xl font-semibold">Semantic search</h1>
        <p className="max-w-prose text-slate-800">
          Find pages by meaning, using the stored embeddings of their current versions. The query is
          embedded by the local embedding model when you search; it is not stored.
        </p>
      </header>

      <FilterForm
        action={PATH}
        label="Semantic search"
        submitLabel="Search"
        clearLabel="Clear search"
        {...(query.kind !== "empty" ? { clearHref: PATH } : {})}
      >
        <TextField
          id="search-q"
          name="q"
          label="Query"
          value={shownQuery}
          minLength={SEARCH_MIN_LENGTH}
          maxLength={SEARCH_MAX_LENGTH}
          describedBy="search-q-hint"
          placeholder="e.g. electric vehicle batteries in Asia"
        />
        <SelectField
          id="search-limit"
          name="limit"
          label="Number of results"
          value={limit === SEARCH_DEFAULT_LIMIT ? undefined : String(limit)}
          anyLabel={`${SEARCH_DEFAULT_LIMIT} (default)`}
          options={SEARCH_LIMITS.filter((value) => value !== SEARCH_DEFAULT_LIMIT).map((value) => ({
            value: String(value),
            label: String(value),
          }))}
        />
        <p id="search-q-hint" className="basis-full text-xs text-slate-700">
          {SEARCH_MIN_LENGTH}–{SEARCH_MAX_LENGTH} characters.
        </p>
      </FilterForm>

      <section aria-labelledby="results-heading" className="space-y-3">
        <h2 id="results-heading" className="text-xl font-semibold">
          Results
        </h2>
        {query.kind === "empty" ? (
          <p role="status" className="text-slate-700">
            Enter a query to search the analysed pages.
          </p>
        ) : query.kind === "invalid" ? (
          <p role="status" className="rounded border border-amber-700 bg-amber-50 p-3 text-amber-950">
            The query was not searched. {query.message}
          </p>
        ) : searchResult === null ? null : !searchResult.ok ? (
          isModelUnavailableError(searchResult.error) ? (
            <ModelUnavailableNotice error={searchResult.error} />
          ) : (
            <ApiErrorNotice error={searchResult.error} what="Semantic search" />
          )
        ) : (
          <SimilarPageTable
            pages={searchResult.data.items}
            caption={`Results for “${query.q}”`}
            labels={labels}
            scoreHeader="Similarity"
            emptyMessage="No pages matched. Pages appear here once they have a fresh embedding."
          />
        )}
      </section>
    </div>
  );
}
