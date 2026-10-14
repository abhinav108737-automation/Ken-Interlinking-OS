import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { PolicyDocumentView } from "@/components/audit/PolicyDocumentView";
import { NotPermitted } from "@/components/auth/NotPermitted";
import { getPolicies, requireSessionUser } from "@/lib/api";
import { can, permissionsOf } from "@/lib/auth";
import { describeAutoApprove } from "@/lib/status";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Settings: policies",
};

export default async function PoliciesPage() {
  const me = await requireSessionUser();
  const header = (
    <header className="space-y-2">
      <h1 className="text-2xl font-semibold">Settings: active policies</h1>
      <p className="max-w-prose text-slate-800">
        The four policy documents the engine and agents run with, as validated by the API. Read-only: policies change
        only through a reviewed change to the repository, never from this console. See also{" "}
        <Link href="/system" className="text-blue-800 underline underline-offset-4">
          system status
        </Link>
        .
      </p>
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
  if (!can(permissionsOf(me.data), "read_settings")) {
    return (
      <div className="space-y-6">
        {header}
        <NotPermitted permission="read_settings" what="Settings" />
      </div>
    );
  }
  const result = await getPolicies();
  return (
    <div className="space-y-6">
      {header}
      {!result.ok ? (
        <ApiErrorNotice error={result.error} what="Policies" />
      ) : (
        <>
          <p className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">AUTO_APPROVE:</span>
            <StatusBadge presentation={describeAutoApprove(result.data.auto_approve_enabled)} />
            <span className="text-slate-700">{describeAutoApprove(result.data.auto_approve_enabled).description}</span>
          </p>
          {result.data.policies.length === 0 ? (
            <p role="status" className="text-slate-700">
              The API reported no active policy documents.
            </p>
          ) : (
            result.data.policies.map((policy) => <PolicyDocumentView key={`${policy.kind}:${policy.path}`} policy={policy} />)
          )}
        </>
      )}
    </div>
  );
}
