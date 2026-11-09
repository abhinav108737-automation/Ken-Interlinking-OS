import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { Pagination } from "@/components/inventory/Pagination";
import { ModuleTable } from "@/components/recommendations/ModuleTable";
import { listPageModules } from "@/lib/api";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseOffset } from "@/lib/inventory";
import { MODULES_PATH, RECOMMENDATIONS_PATH } from "@/lib/recommendations";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Related-report modules",
};

const EMPTY = "No modules proposed. Run make modules after an analysis, or make recommend for both.";
const NAME = "Related-report modules";

export default async function ModulesPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const offset = parseOffset(params, "offset");
  const result = await listPageModules({ limit: DEFAULT_PAGE_SIZE, offset });

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href={RECOMMENDATIONS_PATH} className="text-blue-800 underline underline-offset-4">
            ← Recommendations
          </Link>
        </p>
        <h1 className="text-2xl font-semibold">{NAME}</h1>
        <p className="max-w-prose text-slate-800">
          The same market in every other geography we cover, ranked: a region containing the reader&rsquo;s geography
          first, then global, then a neighbour in the same region, then elsewhere. The link text is the target&rsquo;s
          own title, so it names the geography without rewriting a sentence — which is why a module can express what an
          inline anchor cannot.
        </p>
        <p className="max-w-prose text-slate-800">
          Read-only, and a proposal only: nothing here is published, and how a module is composed onto a page is not yet
          decided. Rebuilt by <code>make modules</code>, which takes seconds, or by <code>make recommend</code>, which
          rebuilds the inline plans as well.
        </p>
      </header>

      {result.ok ? (
        <>
          <ModuleTable caption={NAME} modules={result.data.items} total={result.data.page.total} emptyMessage={EMPTY} />
          <Pagination
            page={result.data.page}
            itemCount={result.data.items.length}
            pathname={MODULES_PATH}
            params={{}}
            offsetParam="offset"
            label={`${NAME} pagination`}
          />
        </>
      ) : (
        <ApiErrorNotice error={result.error} what={NAME} />
      )}
    </div>
  );
}
