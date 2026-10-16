import type { Metadata } from "next";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { AutonomyModeBanner } from "@/components/autonomy/AutonomyModeBanner";
import { DecisionTable } from "@/components/autonomy/DecisionTable";
import { FilterForm, SelectField, TextField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { getAutonomyMode, listAutonomyDecisions } from "@/lib/api";
import {
  AUTONOMY_DECISIONS,
  AUTONOMY_MODES,
  DECISIONS_PATH,
  describeAutonomyDecision,
  describeAutonomyModeShort,
  parseDecisionFilter,
  parseModeFilter,
} from "@/lib/autonomy";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseLimit, parseOffset } from "@/lib/inventory";
import { parseUuidParam, paramText, paramValue } from "@/lib/recommendations";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Autonomy decisions",
};

const DECISION_OPTIONS = AUTONOMY_DECISIONS.map((value) => ({
  value,
  label: describeAutonomyDecision(value).label,
}));

const MODE_OPTIONS = AUTONOMY_MODES.map((value) => ({
  value,
  label: value === "shadow" ? "Shadow (counterfactual)" : describeAutonomyModeShort(value),
}));

export default async function AutonomyDecisionsPage({
  searchParams,
}: {
  readonly searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const decision = parseDecisionFilter(params);
  const mode = parseModeFilter(params);
  const plan = parseUuidParam(params, "plan_id");
  const limit = parseLimit(params);
  const offset = parseOffset(params);
  const filtered = decision !== undefined || mode !== undefined || plan.kind !== "absent";

  const [result, health] = await Promise.all([
    listAutonomyDecisions({
      decision: decision ?? null,
      mode: mode ?? null,
      plan_id: paramValue(plan) ?? null,
      limit,
      offset,
    }),
    getAutonomyMode(),
  ]);

  const state = {
    decision,
    mode,
    plan_id: paramValue(plan),
    limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit,
  };

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Autonomy decisions</h1>
        <AutonomyModeBanner health={health.ok ? health.data : null} compact />
        <p className="max-w-prose text-slate-800">
          Every evaluation the autonomy engine recorded, newest first: the decision, the mode it was taken in, the risk
          tier, the confidence it was taken on and the deterministic blocks that fired. A decision taken in{" "}
          <strong>shadow</strong> mode did not happen — it is a counterfactual record and nothing on the site changed.
        </p>
      </header>

      <section aria-labelledby="decisions-heading" className="space-y-3">
        <h2 id="decisions-heading" className="text-xl font-semibold">
          Decision log
        </h2>
        <FilterForm
          action={DECISIONS_PATH}
          label="Filter autonomy decisions"
          {...(filtered ? { clearHref: DECISIONS_PATH } : {})}
        >
          <SelectField
            id="decisions-decision"
            name="decision"
            label="Decision"
            value={decision ?? ""}
            options={DECISION_OPTIONS}
          />
          <SelectField id="decisions-mode" name="mode" label="Mode" value={mode ?? ""} options={MODE_OPTIONS} />
          <TextField
            id="decisions-plan"
            name="plan_id"
            label="Recommendation id"
            value={paramText(plan)}
            maxLength={80}
          />
        </FilterForm>
        {plan.kind === "invalid" && (
          <p role="alert" className="rounded border border-amber-700 bg-amber-50 p-3 text-sm text-amber-950">
            <span aria-hidden="true">! </span>
            The recommendation id filter was ignored: it must be a UUID.
          </p>
        )}
        {result.ok ? (
          <>
            <DecisionTable decisions={result.data.items} total={result.data.page.total} />
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={DECISIONS_PATH}
              params={state}
              label="Autonomy decisions pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={result.error} what="Autonomy decisions" />
        )}
      </section>
    </div>
  );
}
