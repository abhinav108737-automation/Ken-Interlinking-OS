import type { Metadata } from "next";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { CorpusBanner } from "@/components/learning/CorpusBanner";
import { ProposalTable } from "@/components/learning/ProposalTable";
import { FilterForm, SelectField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { getLearningOverview, listLearningProposals } from "@/lib/api";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseLimit, parseOffset } from "@/lib/inventory";
import {
  PROPOSALS_PATH,
  PROPOSAL_STATUSES,
  describeProposalStatus,
  parseProposalKind,
  parseProposalStatus,
} from "@/lib/learning";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Learning proposals",
};

const STATUS_OPTIONS = PROPOSAL_STATUSES.map((value) => ({ value, label: describeProposalStatus(value).label }));
const KIND_OPTIONS = [
  { value: "policy", label: "Policy" },
  { value: "skill", label: "Skill" },
];

export default async function ProposalsPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const status = parseProposalStatus(params);
  const kind = parseProposalKind(params);
  const limit = parseLimit(params);
  const offset = parseOffset(params);
  const filtered = status !== undefined || kind !== undefined;

  const [result, overview] = await Promise.all([
    listLearningProposals({ status: status ?? null, kind: kind ?? null, limit, offset }),
    getLearningOverview(),
  ]);

  const state = { status, kind, limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit };

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Learning proposals</h1>
        {overview.ok && <CorpusBanner banner={overview.data.banner} />}
        <p className="max-w-prose text-slate-800">
          Proposed changes to a policy document or a skill, each with the evidence it was built from. A proposal changes
          nothing by existing: it must pass an offline evaluation and then be promoted by a human.
        </p>
      </header>

      <section aria-labelledby="proposals-heading" className="space-y-3">
        <h2 id="proposals-heading" className="text-xl font-semibold">
          Proposals
        </h2>
        <FilterForm
          action={PROPOSALS_PATH}
          label="Filter learning proposals"
          {...(filtered ? { clearHref: PROPOSALS_PATH } : {})}
        >
          <SelectField id="proposals-status" name="status" label="Status" value={status ?? ""} options={STATUS_OPTIONS} />
          <SelectField id="proposals-kind" name="kind" label="Kind" value={kind ?? ""} options={KIND_OPTIONS} />
        </FilterForm>
        {result.ok ? (
          <>
            <ProposalTable proposals={result.data.items} total={result.data.page.total} />
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={PROPOSALS_PATH}
              params={state}
              label="Proposals pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={result.error} what="Proposals" />
        )}
      </section>
    </div>
  );
}
