import type { Metadata } from "next";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { AuditNav } from "@/components/audit/AuditNav";
import { NotPermitted } from "@/components/auth/NotPermitted";
import { FilterForm, SelectField, TextField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { ReviewActionTable } from "@/components/review/ReviewActionTable";
import { listReviewActions, requireSessionUser } from "@/lib/api";
import { AUDIT_REVIEWS_PATH } from "@/lib/audit";
import { can, permissionsOf } from "@/lib/auth";
import { DEFAULT_PAGE_SIZE, type SearchParams, firstParam, parseEnumParam, parseLimit, parseOffset } from "@/lib/inventory";
import { REVIEW_ACTIONS, describeReviewAction } from "@/lib/review";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Audit: review actions",
};

/** `actor` filter limit, as the contract declares it. */
const MAX_ACTOR = 210;

export default async function AuditReviewsPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const me = await requireSessionUser();
  const header = (
    <header className="space-y-2">
      <h1 className="text-2xl font-semibold">Audit log: review actions</h1>
      <p className="max-w-prose text-slate-800">
        Every human review action, newest first: who acted, the reasons and note they gave, and what changed. This is
        also the editorial feedback that later learning phases read. Read-only.
      </p>
      <AuditNav current="reviews" />
    </header>
  );
  if (!me.ok) {
    return (
      <div className="space-y-6">
        {header}
        <ApiErrorNotice error={me.error} what="Session" />
      </div>
    );
  }
  if (!can(permissionsOf(me.data), "read_audit")) {
    return (
      <div className="space-y-6">
        {header}
        <NotPermitted permission="read_audit" what="The audit log" />
      </div>
    );
  }

  const action = parseEnumParam(params, "action", REVIEW_ACTIONS);
  const actor = firstParam(params, "actor")?.slice(0, MAX_ACTOR);
  const limit = parseLimit(params);
  const offset = parseOffset(params);
  const result = await listReviewActions({ action: action ?? null, actor: actor ?? null, limit, offset });
  const filtered = action !== undefined || actor !== undefined;
  const state = { action, actor, limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit };

  return (
    <div className="space-y-6">
      {header}
      <FilterForm action={AUDIT_REVIEWS_PATH} label="Filter review actions" {...(filtered ? { clearHref: AUDIT_REVIEWS_PATH } : {})}>
        <SelectField
          id="audit-action"
          name="action"
          label="Action"
          value={action}
          options={REVIEW_ACTIONS.map((value) => ({ value, label: describeReviewAction(value) }))}
        />
        <TextField
          id="audit-actor"
          name="actor"
          label="Actor"
          value={actor}
          maxLength={MAX_ACTOR}
          placeholder="user:jane"
        />
      </FilterForm>
      {!result.ok ? (
        <ApiErrorNotice error={result.error} what="Review actions" />
      ) : (
        <>
          <ReviewActionTable
            actions={result.data.items}
            caption="Review actions"
            total={result.data.page.total}
            emptyMessage={filtered ? "No review actions match these filters." : "No reviewer has acted yet."}
          />
          <Pagination
            page={result.data.page}
            itemCount={result.data.items.length}
            pathname={AUDIT_REVIEWS_PATH}
            params={state}
            label="Review actions pagination"
          />
        </>
      )}
    </div>
  );
}
