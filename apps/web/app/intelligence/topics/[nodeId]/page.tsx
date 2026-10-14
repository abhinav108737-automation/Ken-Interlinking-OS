import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { SimilarPageTable } from "@/components/intelligence/SimilarPageTable";
import { Pagination } from "@/components/inventory/Pagination";
import { getTaxonomy, isMissingResourceError, listTaxonomyNodePages } from "@/lib/api";
import { describeNodeKind, nodeLabel, taxonomyLabels, taxonomyNodeHref } from "@/lib/intelligence";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseLimit, parseOffset } from "@/lib/inventory";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Taxonomy node",
};

export default async function TaxonomyNodePage({
  params,
  searchParams,
}: {
  readonly params: Promise<{ nodeId: string }>;
  readonly searchParams: Promise<SearchParams>;
}) {
  const [{ nodeId }, query] = await Promise.all([params, searchParams]);
  const limit = parseLimit(query);
  const offset = parseOffset(query);

  const [pagesResult, taxonomyResult] = await Promise.all([
    listTaxonomyNodePages(nodeId, { limit, offset }),
    getTaxonomy(),
  ]);

  if (!pagesResult.ok && isMissingResourceError(pagesResult.error)) {
    notFound();
  }

  const taxonomy = taxonomyResult.ok ? taxonomyResult.data : null;
  const labels = taxonomyLabels(taxonomy);
  const node = taxonomy === null ? undefined : [...taxonomy.markets, ...taxonomy.geographies].find((n) => n.id === nodeId);
  const self = taxonomyNodeHref(nodeId);

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href="/intelligence" className="text-blue-800 underline underline-offset-4">
            ← Content intelligence
          </Link>
        </p>
        <h1 className="break-words text-2xl font-semibold">{nodeLabel(labels, nodeId)}</h1>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
          <dt className="font-medium text-slate-700">Node id</dt>
          <dd>
            <code className="text-xs">{nodeId}</code>
          </dd>
          {node !== undefined && (
            <>
              <dt className="font-medium text-slate-700">Kind</dt>
              <dd>{describeNodeKind(node.kind)}</dd>
              <dt className="font-medium text-slate-700">Parent</dt>
              <dd>
                {node.parent === null ? (
                  <span className="text-slate-600">None (top level)</span>
                ) : (
                  <Link href={taxonomyNodeHref(node.parent)} className="text-blue-800 underline underline-offset-2">
                    {nodeLabel(labels, node.parent)}
                  </Link>
                )}
              </dd>
            </>
          )}
        </dl>
        <p className="max-w-prose text-slate-800">
          Pages whose current, fresh analysis lists this node, highest score first.
        </p>
      </header>

      <section aria-labelledby="node-pages-heading" className="space-y-3">
        <h2 id="node-pages-heading" className="sr-only">
          Pages
        </h2>
        {pagesResult.ok ? (
          <>
            <SimilarPageTable
              pages={pagesResult.data.items}
              total={pagesResult.data.page.total}
              caption="Pages"
              labels={labels}
              emptyMessage={
                offset > 0 ? "No pages on this page of results." : "No analysed page lists this node yet."
              }
            />
            <Pagination
              page={pagesResult.data.page}
              itemCount={pagesResult.data.items.length}
              pathname={self}
              params={{ limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit }}
              label="Pages pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={pagesResult.error} what="Node pages" />
        )}
      </section>
    </div>
  );
}
