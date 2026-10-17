import type { Metadata } from "next";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { AutonomyModeBanner } from "@/components/autonomy/AutonomyModeBanner";
import { BlockedActionTable } from "@/components/autonomy/BlockedActionTable";
import { FilterForm, SelectField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { getAutonomyMode, listBlockedActions, requireSessionUser } from "@/lib/api";
import { can, permissionsOf } from "@/lib/auth";
import { BLOCKED_PATH, BLOCK_CODES, describeBlockCode, parseBlockCodeFilter } from "@/lib/autonomy";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseLimit, parseOffset } from "@/lib/inventory";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Blocked actions",
};

const CODE_OPTIONS = BLOCK_CODES.map((value) => ({ value, label: describeBlockCode(value) }));

export default async function BlockedActionsPage({
  searchParams,
}: {
  readonly searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const code = parseBlockCodeFilter(params);
  const limit = parseLimit(params);
  const offset = parseOffset(params);

  const [result, health, me] = await Promise.all([
    listBlockedActions({ code: code ?? null, limit, offset }),
    getAutonomyMode(),
    requireSessionUser(),
  ]);
  const canOverride = can(permissionsOf(me.ok ? me.data : null), "deploy");

  const state = { code, limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit };

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Blocked actions</h1>
        <AutonomyModeBanner health={health.ok ? health.data : null} compact />
        <p className="max-w-prose text-slate-800">
          Plans that were not published. A <strong>hard block</strong> is a deterministic safety rule: no model call, no
          judgement, and <strong>never overridable into a publish</strong>. A <strong>deferral</strong> has no block
          code: guarded mode held the plan back as too risky, uncalibrated or under-confident, and it is recorded here
          as blocked (not as awaiting approval). Open a row to investigate it: the recorded reason, what to check and
          the plan&apos;s own evidence.
        </p>
        <p className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
          {canOverride
            ? "Your role may record an operator override on a blocked plan. It is an exception, not the normal path, and it records your account and your reason."
            : "Recording an operator override needs the deploy permission, which your role does not have. You can still investigate every block here."}
        </p>
      </header>

      <section aria-labelledby="blocked-heading" className="space-y-3">
        <h2 id="blocked-heading" className="text-xl font-semibold">
          Blocked plans
        </h2>
        <FilterForm
          action={BLOCKED_PATH}
          label="Filter blocked actions"
          {...(code === undefined ? {} : { clearHref: BLOCKED_PATH })}
        >
          <SelectField
            id="blocked-code"
            name="code"
            label="Block code"
            value={code ?? ""}
            options={CODE_OPTIONS}
            anyLabel="Any block code"
          />
        </FilterForm>
        {result.ok ? (
          <>
            <BlockedActionTable
              blocked={result.data.items}
              total={result.data.page.total}
              caption={code === undefined ? "Blocked actions" : `Blocked actions: ${describeBlockCode(code)}`}
            />
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={BLOCKED_PATH}
              params={state}
              label="Blocked actions pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={result.error} what="Blocked actions" />
        )}
      </section>
    </div>
  );
}
