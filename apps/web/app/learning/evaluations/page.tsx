import type { Metadata } from "next";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { CorpusBanner } from "@/components/learning/CorpusBanner";
import { EvaluationTable } from "@/components/learning/EvaluationTable";
import { FilterForm, SelectField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { getLearningOverview, listLearningEvaluations } from "@/lib/api";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseLimit, parseOffset } from "@/lib/inventory";
import { EVALUATIONS_PATH, GATE_RESULTS, describeGateResult, parseGateResult } from "@/lib/learning";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Evaluation runs",
};

const GATE_OPTIONS = GATE_RESULTS.map((value) => ({ value, label: describeGateResult(value).label }));

export default async function EvaluationsPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const gateResult = parseGateResult(params);
  const limit = parseLimit(params);
  const offset = parseOffset(params);

  const [result, overview] = await Promise.all([
    listLearningEvaluations({ gate_result: gateResult ?? null, limit, offset }),
    getLearningOverview(),
  ]);

  const state = { gate_result: gateResult, limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit };

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Evaluation runs</h1>
        {overview.ok && <CorpusBanner banner={overview.data.banner} />}
        <p className="max-w-prose text-slate-800">
          Learning outcomes: what each evaluation run measured and whether it passed its gate. A gate that cannot reach
          a verdict is <strong>inconclusive</strong>, which is never treated as a pass — below the sample floor there is
          not enough data to decide.
        </p>
      </header>

      <section aria-labelledby="evaluations-heading" className="space-y-3">
        <h2 id="evaluations-heading" className="text-xl font-semibold">
          Runs
        </h2>
        <FilterForm
          action={EVALUATIONS_PATH}
          label="Filter evaluation runs"
          {...(gateResult === undefined ? {} : { clearHref: EVALUATIONS_PATH })}
        >
          <SelectField
            id="evaluations-gate"
            name="gate_result"
            label="Gate result"
            value={gateResult ?? ""}
            options={GATE_OPTIONS}
          />
        </FilterForm>
        {result.ok ? (
          <>
            <EvaluationTable evaluations={result.data.items} total={result.data.page.total} />
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={EVALUATIONS_PATH}
              params={state}
              label="Evaluation runs pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={result.error} what="Evaluation runs" />
        )}
      </section>
    </div>
  );
}
