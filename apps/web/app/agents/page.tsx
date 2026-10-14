import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { AgentRunTable } from "@/components/agents/AgentRunTable";
import { FilterForm, SelectField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import {
  AGENTS_PATH,
  AGENT_OUTCOMES,
  AGENT_RUN_STATUSES,
  describeAgentOutcome,
  describeAgentRunStatus,
} from "@/lib/agents";
import { listAgentRuns } from "@/lib/api";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseEnumParam, parseLimit, parseOffset } from "@/lib/inventory";

// Agent runs are live data: never prerender or cache this page.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Agent runs",
};

export default async function AgentRunsPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const status = parseEnumParam(params, "status", AGENT_RUN_STATUSES);
  const outcome = parseEnumParam(params, "outcome", AGENT_OUTCOMES);
  const limit = parseLimit(params);
  const offset = parseOffset(params);
  const filtered = status !== undefined || outcome !== undefined;

  const result = await listAgentRuns({ status: status ?? null, outcome: outcome ?? null, limit, offset });

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Agent runs</h1>
        <p className="max-w-prose text-slate-800">
          Supervised agent reviews of the recommendation engine&apos;s proposed plans, newest first. An agent run
          reviews one source page&apos;s plans and hands the ones worth a person&apos;s time to the review queue
          (status <em>Awaiting approval</em>). Agents never create, approve, reject or publish a link. Runs start from
          the command line (<code>make agents</code>); this view is read-only. Each run belongs to an agent batch listed
          under{" "}
          <Link href="/jobs?kind=agent" className="text-blue-800 underline underline-offset-4">
            Jobs
          </Link>
          .
        </p>
      </header>

      <FilterForm action={AGENTS_PATH} label="Filter agent runs" {...(filtered ? { clearHref: AGENTS_PATH } : {})}>
        <SelectField
          id="agents-status"
          name="status"
          label="Status"
          value={status}
          options={AGENT_RUN_STATUSES.map((value) => ({ value, label: describeAgentRunStatus(value).label }))}
        />
        <SelectField
          id="agents-outcome"
          name="outcome"
          label="Outcome"
          value={outcome}
          options={AGENT_OUTCOMES.map((value) => ({ value, label: describeAgentOutcome(value).label }))}
        />
      </FilterForm>

      <section aria-labelledby="agent-runs-heading" className="space-y-3">
        <h2 id="agent-runs-heading" className="sr-only">
          Agent runs
        </h2>
        {!result.ok ? (
          <ApiErrorNotice error={result.error} what="Agent runs" />
        ) : result.data.page.total === 0 && result.data.items.length === 0 ? (
          <div role="status" className="rounded border border-slate-300 bg-slate-50 p-4 text-slate-800">
            {filtered ? (
              <p>No agent runs match these filters.</p>
            ) : (
              <p>
                No agent runs yet — run <code>make agents</code> after <code>make recommend</code>.
              </p>
            )}
          </div>
        ) : (
          <>
            {result.data.items.length === 0 ? (
              <p role="status" className="text-slate-700">
                No agent runs on this page of results.
              </p>
            ) : (
              <AgentRunTable runs={result.data.items} total={result.data.page.total} />
            )}
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={AGENTS_PATH}
              params={{ status, outcome, limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit }}
              label="Agent runs pagination"
            />
          </>
        )}
      </section>
    </div>
  );
}
