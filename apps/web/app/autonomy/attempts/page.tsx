import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { AutonomyModeBanner } from "@/components/autonomy/AutonomyModeBanner";
import { PublishAttemptTable } from "@/components/autonomy/PublishAttemptTable";
import { FilterForm, TextField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { getAutonomyMode, listPublishAttempts } from "@/lib/api";
import { ATTEMPTS_PATH } from "@/lib/autonomy";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseLimit, parseOffset } from "@/lib/inventory";
import { paramText, paramValue, parseUuidParam } from "@/lib/recommendations";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Publish attempts",
};

export default async function PublishAttemptsPage({
  searchParams,
}: {
  readonly searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const plan = parseUuidParam(params, "plan_id");
  const limit = parseLimit(params);
  const offset = parseOffset(params);

  const [result, health] = await Promise.all([
    listPublishAttempts({ plan_id: paramValue(plan) ?? null, limit, offset }),
    getAutonomyMode(),
  ]);

  const state = { plan_id: paramValue(plan), limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit };

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Publish attempts and rollbacks</h1>
        <AutonomyModeBanner health={health.ok ? health.data : null} compact />
        <p className="max-w-prose text-slate-800">
          The append-only ledger of what the autonomy engine wrote to the site: what published, what was rolled back,
          what failed and with which code. Every autonomous publish is verified; on a verification failure the system
          rolls back by itself when that is safe, records the failure and puts the pair in a cool-down so the same plan
          is not retried in a loop. Deployment-level history is under{" "}
          <Link href="/deployments" className="text-blue-800 underline underline-offset-4">
            deployments
          </Link>
          .
        </p>
      </header>

      <section aria-labelledby="attempts-heading" className="space-y-3">
        <h2 id="attempts-heading" className="text-xl font-semibold">
          Attempt ledger
        </h2>
        <FilterForm
          action={ATTEMPTS_PATH}
          label="Filter publish attempts"
          {...(plan.kind === "absent" ? {} : { clearHref: ATTEMPTS_PATH })}
        >
          <TextField
            id="attempts-plan"
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
            <PublishAttemptTable attempts={result.data.items} total={result.data.page.total} />
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={ATTEMPTS_PATH}
              params={state}
              label="Publish attempts pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={result.error} what="Publish attempts" />
        )}
      </section>
    </div>
  );
}
