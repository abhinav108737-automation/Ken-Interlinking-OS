import Link from "next/link";

import type { GraphOverview } from "@/lib/api-client";
import { formatCount, formatDateTime } from "@/lib/format";
import { GRAPH_PAGES_PATH } from "@/lib/graph";
import { buildHref } from "@/lib/inventory";

/**
 * Graph overview. TRUE orphans are the headline; the raw `orphans` count is
 * shown only in a sentence that says it includes the crawl entry page, so it is
 * never read as the number of problem pages.
 */
export function GraphOverviewPanel({ overview }: { readonly overview: GraphOverview }) {
  if (overview.metrics_version === null) {
    return (
      <p role="status" className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-slate-800">
        Graph metrics have never been computed. Run <code>make graph-metrics</code>.
      </p>
    );
  }
  const tiles: readonly (readonly [string, number, string | null, string])[] = [
    ["True orphans", overview.true_orphans, buildHref(GRAPH_PAGES_PATH, { orphans_only: 1 }), "No inbound link, and not the crawl entry page."],
    ["Nearly orphans", overview.nearly_orphans, null, "Inbound links at or below the policy threshold."],
    ["Bridge pages", overview.bridges, buildHref(GRAPH_PAGES_PATH, { bridges_only: 1 }), "Cut vertices of the link graph."],
    ["Entry pages", overview.entry_pages, null, "Crawl depth 0: not problem pages."],
    ["Pages with metrics", overview.pages, GRAPH_PAGES_PATH, "Live pages, current version."],
    ["Components", overview.components, null, "Weakly connected components."],
    ["Topical clusters", overview.clusters, null, "Pages with no topic belong to none."],
  ];
  return (
    <div className="space-y-3">
      <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {tiles.map(([term, value, href, hint]) => (
          <div key={term} className="rounded border border-slate-300 bg-white p-3">
            <dt className="text-sm text-slate-700">{term}</dt>
            <dd className="text-2xl font-semibold tabular-nums">
              {href === null ? (
                formatCount(value)
              ) : (
                <Link href={href} className="text-blue-800 underline underline-offset-4">
                  {formatCount(value)}
                </Link>
              )}
            </dd>
            <dd className="text-xs text-slate-700">{hint}</dd>
          </div>
        ))}
      </dl>
      <p className="max-w-prose text-sm text-slate-800">
        The database flags {formatCount(overview.orphans)} page{overview.orphans === 1 ? "" : "s"} as{" "}
        <code>is_orphan</code>. That count <strong>includes the crawl entry page</strong> when nothing links to it, so
        it is not the number of problem pages: {formatCount(overview.true_orphans)} true orphan
        {overview.true_orphans === 1 ? "" : "s"} plus {formatCount(overview.entry_pages)} entry page
        {overview.entry_pages === 1 ? "" : "s"}.
      </p>
      <p className="text-xs text-slate-700">
        Metrics version <code>{overview.metrics_version}</code>
        {overview.computed_at !== null && (
          <>
            , computed <time dateTime={overview.computed_at}>{formatDateTime(overview.computed_at)}</time>
          </>
        )}
        .
      </p>
    </div>
  );
}
