import type { Metadata } from "next";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { DependencyTable } from "@/components/DependencyTable";
import { StatusBadge } from "@/components/StatusBadge";
import { AutonomyModeBanner } from "@/components/autonomy/AutonomyModeBanner";
import { getAutonomyMode, getMeta, getReady, type ServiceIdentity } from "@/lib/api";
import {
  describeAutoApprove,
  describeHealthStatus,
  describeReadinessStatus,
  formatLatency,
  keyCheckGroups,
  requirementLabel,
  summarizeCheck,
} from "@/lib/status";

// Readiness is live state: never prerender or cache this page.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "System status",
};

function IdentityRow({ term, children }: { term: string; children: ReactNode }) {
  return (
    <>
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd className="font-mono text-slate-900">{children}</dd>
    </>
  );
}

export default async function SystemPage() {
  // Each call returns a result and never throws, so one failure cannot blank the page.
  const [ready, meta, mode] = await Promise.all([getReady(), getMeta(), getAutonomyMode()]);

  const identity: ServiceIdentity | null = meta.ok
    ? meta.data.identity
    : ready.ok
      ? ready.data.identity
      : null;
  const apiUnreachable = !ready.ok && !meta.ok;

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">System status</h1>
        <p className="max-w-prose text-slate-800">
          Live readiness of the control API and its dependencies — including the vector store
          (Qdrant), the model server (Ollama) and the CMS provider — checked on the server when this
          page was requested. Reload the page to check again.
        </p>
      </header>

      <section aria-labelledby="autonomy-heading" className="space-y-3">
        <h2 id="autonomy-heading" className="text-xl font-semibold">
          Autonomy
        </h2>
        {mode.ok ? (
          <AutonomyModeBanner health={mode.data} />
        ) : (
          <ApiErrorNotice error={mode.error} what="Autonomy mode" />
        )}
      </section>

      {apiUnreachable && (
        <p role="status" className="max-w-prose font-medium text-red-900">
          The control API could not be queried. Confirm the API process is running and that the
          web server&apos;s KEN_API_BASE_URL points at it.
        </p>
      )}

      <section aria-labelledby="readiness-heading" className="space-y-3">
        <h2 id="readiness-heading" className="text-xl font-semibold">
          Aggregate readiness
        </h2>
        {ready.ok ? (
          <div className="space-y-2">
            <p className="flex flex-wrap items-center gap-2">
              <span className="font-medium">Status:</span>
              <StatusBadge presentation={describeReadinessStatus(ready.data.status)} />
              <span className="text-slate-700">
                {describeReadinessStatus(ready.data.status).description}
              </span>
            </p>
            <p className="text-sm text-slate-700">
              HTTP {ready.status} · checked at{" "}
              <time dateTime={ready.data.timestamp}>{ready.data.timestamp}</time> · took{" "}
              {formatLatency(ready.data.duration_ms)}
            </p>
          </div>
        ) : (
          <ApiErrorNotice error={ready.error} what="Readiness" />
        )}
      </section>

      <section aria-labelledby="identity-heading" className="space-y-3">
        <h2 id="identity-heading" className="text-xl font-semibold">
          Service identity
        </h2>
        {identity !== null ? (
          <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
            <IdentityRow term="Service">{identity.service}</IdentityRow>
            <IdentityRow term="Version">{identity.version}</IdentityRow>
            <IdentityRow term="Environment">{identity.environment}</IdentityRow>
            <IdentityRow term="Git SHA">{identity.git_sha ?? "unknown"}</IdentityRow>
            {meta.ok && (
              <>
                <IdentityRow term="Phase">{meta.data.phase}</IdentityRow>
                <IdentityRow term="Python">{meta.data.python_version}</IdentityRow>
                <dt className="font-medium text-slate-700">Auto-approve</dt>
                <dd>
                  <StatusBadge presentation={describeAutoApprove(meta.data.auto_approve_enabled)} />
                </dd>
              </>
            )}
          </dl>
        ) : (
          <p className="text-slate-700">Service identity is unknown because the API did not respond.</p>
        )}
        {!meta.ok && <ApiErrorNotice error={meta.error} what="Build metadata" />}
      </section>

      <section aria-labelledby="key-services-heading" className="space-y-3">
        <h2 id="key-services-heading" className="text-xl font-semibold">
          Vector store, model server and CMS
        </h2>
        <p className="max-w-prose text-sm text-slate-700">
          The three services an operator asks about by name. Qdrant and Ollama are optional by
          default, so their absence degrades the service without taking the API down; a service the
          API did not report at all is shown as such rather than silently omitted.
        </p>
        {ready.ok ? (
          <dl className="grid gap-3 lg:grid-cols-3">
            {keyCheckGroups(ready.data.checks).map((group) => (
              <div key={group.kind} className="space-y-2 rounded border border-slate-300 bg-white p-3">
                <dt className="font-semibold">{group.label}</dt>
                <dd className="space-y-2">
                  {group.checks.length === 0 ? (
                    <p className="text-sm text-slate-700">
                      Not reported by this API build. <code className="text-xs">{group.kind}</code>
                    </p>
                  ) : (
                    group.checks.map((check) => {
                      const summary = summarizeCheck(check);
                      return (
                        <div key={check.name} className="space-y-1 text-sm">
                          <p className="flex flex-wrap items-center gap-2">
                            <span className="font-mono text-xs">{check.name}</span>
                            <StatusBadge presentation={describeHealthStatus(check.status)} />
                            <span className="text-xs text-slate-700">
                              {requirementLabel(check.required)} · {formatLatency(check.latency_ms)}
                            </span>
                          </p>
                          {summary.error !== null && (
                            <p className="text-xs text-red-900">
                              <span className="font-semibold">Error:</span> {summary.error}
                            </p>
                          )}
                          {summary.detail !== "" && (
                            <p className="break-words font-mono text-xs text-slate-700">{summary.detail}</p>
                          )}
                        </div>
                      );
                    })
                  )}
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="text-slate-700">
            These checks are unavailable because the readiness request failed.
          </p>
        )}
      </section>

      <section aria-labelledby="dependencies-heading" className="space-y-3">
        <h2 id="dependencies-heading" className="text-xl font-semibold">
          Dependencies
        </h2>
        {ready.ok ? (
          <DependencyTable checks={ready.data.checks} />
        ) : (
          <p className="text-slate-700">
            Dependency checks are unavailable because the readiness request failed.
          </p>
        )}
      </section>
    </div>
  );
}
