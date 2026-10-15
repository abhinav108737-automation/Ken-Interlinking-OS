import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { DeploymentTable } from "@/components/deployments/DeploymentTable";
import { FilterForm, SelectField, TextField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { listDeployments } from "@/lib/api";
import { DEPLOYMENTS_PATH, DEPLOYMENT_STATUSES, describeDeploymentStatus } from "@/lib/deployments";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseEnumParam, parseLimit, parseOffset } from "@/lib/inventory";
import { RECOMMENDATIONS_PATH, paramText, paramValue, parseUuidParam } from "@/lib/recommendations";

// Deployments are live data: never prerender or cache this page.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Deployments",
};

export default async function DeploymentsPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const status = parseEnumParam(params, "status", DEPLOYMENT_STATUSES);
  const source = parseUuidParam(params, "source_page_id");
  const limit = parseLimit(params);
  const offset = parseOffset(params);
  const filtered = status !== undefined || source.kind !== "absent";

  const result = await listDeployments({
    status: status ?? null,
    source_page_id: paramValue(source) ?? null,
    limit,
    offset,
  });

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Deployments</h1>
        <p className="max-w-prose text-slate-800">
          Every request to write approved links to the CMS, newest first. A deployment covers up to 20 approved
          recommendations of one source page; it is validated, dry-run, applied once against the CMS page version that
          was checked, verified, and can be rolled back. Nothing deploys automatically: an operator starts each one from
          an approved recommendation in the{" "}
          <Link href={`${RECOMMENDATIONS_PATH}?status=approved`} className="text-blue-800 underline underline-offset-4">
            recommendation inbox
          </Link>
          .
        </p>
      </header>

      <FilterForm action={DEPLOYMENTS_PATH} label="Filter deployments" {...(filtered ? { clearHref: DEPLOYMENTS_PATH } : {})}>
        <SelectField
          id="deployments-status"
          name="status"
          label="Status"
          value={status}
          options={DEPLOYMENT_STATUSES.map((value) => ({ value, label: describeDeploymentStatus(value).label }))}
        />
        <TextField
          id="deployments-source"
          name="source_page_id"
          label="Source page id"
          value={paramText(source)}
          maxLength={80}
        />
      </FilterForm>

      {source.kind === "invalid" && (
        <p role="alert" className="rounded border border-amber-700 bg-amber-50 p-3 text-sm text-amber-950">
          <span aria-hidden="true">! </span>The source page id filter was ignored: it must be a page UUID.
        </p>
      )}

      <section aria-labelledby="deployments-heading" className="space-y-3">
        <h2 id="deployments-heading" className="sr-only">
          Deployment history
        </h2>
        {!result.ok ? (
          <ApiErrorNotice error={result.error} what="Deployments" />
        ) : result.data.page.total === 0 && result.data.items.length === 0 ? (
          <div role="status" className="rounded border border-slate-300 bg-slate-50 p-4 text-slate-800">
            <p>{filtered ? "No deployments match these filters." : "No deployments yet."}</p>
          </div>
        ) : (
          <>
            {result.data.items.length === 0 ? (
              <p role="status" className="text-slate-700">
                No deployments on this page of results.
              </p>
            ) : (
              <DeploymentTable deployments={result.data.items} total={result.data.page.total} caption="Deployment history" />
            )}
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={DEPLOYMENTS_PATH}
              params={{ status, source_page_id: paramValue(source), limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit }}
              label="Deployments pagination"
            />
          </>
        )}
      </section>
    </div>
  );
}
