import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { GraphClusterTable } from "@/components/graph/GraphClusterTable";
import { GraphDefinitions } from "@/components/graph/GraphDefinitions";
import { GraphOverviewPanel } from "@/components/graph/GraphOverviewPanel";
import { getGraphOverview, listGraphClusters } from "@/lib/api";
import { GRAPH_PAGES_PATH } from "@/lib/graph";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "Link graph" };

export default async function GraphPage() {
  const [overview, clusters] = await Promise.all([getGraphOverview(), listGraphClusters()]);
  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Link graph</h1>
        <p className="max-w-prose text-slate-800">
          How the site&apos;s pages link to each other: which pages are unreachable, barely reachable or structurally
          critical, and how pages group into topical clusters. Computed deterministically; see{" "}
          <Link href={GRAPH_PAGES_PATH} className="text-blue-800 underline underline-offset-4">
            all pages
          </Link>
          .
        </p>
        <GraphDefinitions />
      </header>
      <section aria-labelledby="overview-heading" className="space-y-3">
        <h2 id="overview-heading" className="text-xl font-semibold">
          Overview
        </h2>
        {overview.ok ? (
          <GraphOverviewPanel overview={overview.data} />
        ) : (
          <ApiErrorNotice error={overview.error} what="Graph overview" />
        )}
      </section>
      <section aria-labelledby="clusters-heading" className="space-y-3">
        <h2 id="clusters-heading" className="text-xl font-semibold">
          Topical clusters
        </h2>
        {clusters.ok ? (
          <GraphClusterTable clusters={clusters.data} />
        ) : (
          <ApiErrorNotice error={clusters.error} what="Graph clusters" />
        )}
      </section>
    </div>
  );
}
