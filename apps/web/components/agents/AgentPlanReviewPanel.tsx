import Link from "next/link";

import type { AgentPlanReviewList, ApiResult } from "@/lib/api-client";
import { AGENTS_PATH } from "@/lib/agents";

import { ApiErrorNotice } from "../ApiErrorNotice";
import { AgentReviewTable } from "./AgentReviewTable";

/**
 * The agent critic's review of one plan, for its detail screen. A failed fetch
 * is a non-fatal notice: the rest of the plan still renders.
 */
export function AgentPlanReviewPanel({ result }: { readonly result: ApiResult<AgentPlanReviewList> }) {
  if (!result.ok) {
    return <ApiErrorNotice error={result.error} what="Agent review" />;
  }
  if (result.data.length === 0) {
    return (
      <p role="status" className="rounded border border-slate-300 bg-slate-50 p-3 text-slate-800">
        Not reviewed by an agent yet. Agent runs start from the command line (<code>make agents</code>); see{" "}
        <Link href={AGENTS_PATH} className="text-blue-800 underline underline-offset-4">
          agent runs
        </Link>
        .
      </p>
    );
  }
  return (
    <div className="space-y-2">
      <p className="max-w-prose text-sm text-slate-700">
        The critic&apos;s explanation is model output and is shown as plain text. A critic verdict is advice: a person
        still approves or rejects every plan.
      </p>
      <AgentReviewTable
        reviews={result.data}
        caption="Agent review of this plan"
        emptyMessage="Not reviewed by an agent yet."
        showPlan={false}
      />
    </div>
  );
}
