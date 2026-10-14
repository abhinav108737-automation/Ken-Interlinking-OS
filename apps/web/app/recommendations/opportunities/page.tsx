import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { Pagination } from "@/components/inventory/Pagination";
import { OpportunityTable } from "@/components/recommendations/OpportunityTable";
import { type OpportunityKind, listOpportunities } from "@/lib/api";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseOffset } from "@/lib/inventory";
import {
  OPPORTUNITIES_PATH,
  OPPORTUNITY_KINDS,
  RECOMMENDATIONS_PATH,
  describeOpportunityKind,
  describeOpportunityKindDetail,
} from "@/lib/recommendations";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Linking opportunities",
};

const EMPTY: Readonly<Record<OpportunityKind, string>> = {
  orphan: "No orphan pages: every eligible target has at least one inbound internal link.",
  underlinked: "No under-linked commercial pages.",
  link_capacity: "No eligible source pages with spare link capacity.",
};

const offsetParam = (kind: OpportunityKind) => `${kind}_offset`;

export default async function OpportunitiesPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const offsets = Object.fromEntries(
    OPPORTUNITY_KINDS.map((kind) => [kind, parseOffset(params, offsetParam(kind))]),
  ) as Record<OpportunityKind, number>;
  const results = await Promise.all(
    OPPORTUNITY_KINDS.map((kind) => listOpportunities({ kind, limit: DEFAULT_PAGE_SIZE, offset: offsets[kind] })),
  );
  /** Every section's offset, so paging one list keeps the others in place. */
  const state = Object.fromEntries(
    OPPORTUNITY_KINDS.map((kind) => [offsetParam(kind), offsets[kind] === 0 ? undefined : offsets[kind]]),
  );

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href={RECOMMENDATIONS_PATH} className="text-blue-800 underline underline-offset-4">
            ← Recommendations
          </Link>
        </p>
        <h1 className="text-2xl font-semibold">Linking opportunities</h1>
        <p className="max-w-prose text-slate-800">
          Pages the last recommendation run flagged as needing links or able to give them, strongest first, with the
          evidence behind each score. Read-only; recomputed by <code>make recommend</code>.
        </p>
        <nav aria-label="Opportunity kinds">
          <ul className="flex flex-wrap gap-4 text-sm">
            {OPPORTUNITY_KINDS.map((kind) => (
              <li key={kind}>
                <a href={`#${kind}`} className="text-blue-800 underline underline-offset-4">
                  {describeOpportunityKind(kind)}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      </header>

      {OPPORTUNITY_KINDS.map((kind, index) => {
        const result = results[index]!;
        const name = describeOpportunityKind(kind);
        return (
          <section key={kind} id={kind} aria-labelledby={`${kind}-heading`} className="scroll-mt-4 space-y-3">
            <h2 id={`${kind}-heading`} className="text-xl font-semibold">
              {name}
            </h2>
            <p className="text-sm text-slate-700">{describeOpportunityKindDetail(kind)}</p>
            {result.ok ? (
              <>
                <OpportunityTable
                  caption={name}
                  opportunities={result.data.items}
                  total={result.data.page.total}
                  emptyMessage={EMPTY[kind]}
                />
                <Pagination
                  page={result.data.page}
                  itemCount={result.data.items.length}
                  pathname={OPPORTUNITIES_PATH}
                  params={state}
                  offsetParam={offsetParam(kind)}
                  label={`${name} pagination`}
                />
              </>
            ) : (
              <ApiErrorNotice error={result.error} what={name} />
            )}
          </section>
        );
      })}
    </div>
  );
}
