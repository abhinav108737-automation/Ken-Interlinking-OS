import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { ChangePreviewView } from "@/components/deployments/ChangePreviewView";
import { DeploymentActionsPanel } from "@/components/deployments/DeploymentActionsPanel";
import { VerificationRuns } from "@/components/deployments/VerificationRuns";
import { ExternalUrl } from "@/components/inventory/ExternalUrl";
import { pageHref } from "@/components/inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "@/components/inventory/Table";
import { getDeployment, isMissingResourceError, requireSessionUser } from "@/lib/api";
import { can, describeRole, permissionsOf } from "@/lib/auth";
import {
  DEPLOYMENTS_PATH,
  availableDeploymentActions,
  describeDeploymentErrorCode,
  describeDeploymentStatus,
  describeRollbackReason,
  isDeploymentInProgress,
  previewsInOrder,
  rollbackablePlans,
} from "@/lib/deployments";
import { formatDateTime } from "@/lib/format";
import { type SearchParams, firstParam } from "@/lib/inventory";
import { describeActor, describeLinkPlanStatus, planHref } from "@/lib/recommendations";

import { rollbackAction, verifyAction } from "../actions";

const DEPLOYMENT_ACTIONS = { verify: verifyAction, rollback: rollbackAction };

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Deployment detail",
};

const FACTS = "grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm";
const LINK = "text-blue-800 underline underline-offset-2";

function Fact({ term, children }: { readonly term: string; readonly children: ReactNode }) {
  return (
    <>
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd className="min-w-0 break-words text-slate-900">{children}</dd>
    </>
  );
}

function Section({ id, title, children }: { readonly id: string; readonly title: string; readonly children: ReactNode }) {
  return (
    <section aria-labelledby={`${id}-heading`} className="space-y-3">
      <h2 id={`${id}-heading`} className="text-xl font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

function When({ iso, none = "—" }: { readonly iso: string | null | undefined; readonly none?: string }) {
  return iso === null || iso === undefined ? <span className="text-slate-600">{none}</span> : <time dateTime={iso}>{formatDateTime(iso)}</time>;
}

function Version({ value, none }: { readonly value: string | null | undefined; readonly none: string }) {
  return value === null || value === undefined || value === "" ? (
    <span className="text-slate-600">{none}</span>
  ) : (
    <code className="break-all text-xs">{value}</code>
  );
}

export default async function DeploymentDetailPage({
  params,
  searchParams,
}: {
  readonly params: Promise<{ deploymentId: string }>;
  readonly searchParams?: Promise<SearchParams>;
}) {
  const { deploymentId } = await params;
  const query = (await searchParams) ?? {};
  const [result, me] = await Promise.all([getDeployment(deploymentId), requireSessionUser()]);

  if (!result.ok) {
    if (isMissingResourceError(result.error)) {
      notFound();
    }
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Deployment detail</h1>
        <ApiErrorNotice error={result.error} what="Deployment" />
        <p>
          <Link href={DEPLOYMENTS_PATH} className="text-blue-800 underline underline-offset-4">
            Back to deployments
          </Link>
        </p>
      </div>
    );
  }

  const deployment = result.data;
  const status = describeDeploymentStatus(deployment.status);
  const user = me.ok ? me.data : null;
  const canDeploy = can(permissionsOf(user), "deploy");
  const available = availableDeploymentActions(deployment.status);
  const inProgress = isDeploymentInProgress(deployment.status);
  const actor = describeActor(deployment.actor);
  const planLabels = Object.fromEntries(deployment.items.map((item) => [item.plan_id.toLowerCase(), item.anchor_text]));
  const justDeployed = firstParam(query, "deployed") === "1";

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href={DEPLOYMENTS_PATH} className="text-blue-800 underline underline-offset-4">
            ← Deployments
          </Link>
        </p>
        <h1 className="break-words text-2xl font-semibold">Deployment to {deployment.source_url}</h1>
        <div className="flex flex-wrap items-center gap-4">
          <StatusBadge presentation={status} />
          <span className="text-sm text-slate-700">{status.description}</span>
        </div>
        {justDeployed && (
          <p role="status" className="max-w-prose rounded border border-emerald-700 bg-emerald-50 p-2 text-sm text-emerald-950">
            <span aria-hidden="true">✓ </span>
            Your deployment request was recorded
            {deployment.idempotent_replay === true ? " (it repeated an earlier identical request; nothing was applied twice)" : ""}.
            Its current status and verification are shown below.
          </p>
        )}
        {inProgress && (
          <div role="status" className="max-w-prose rounded border border-amber-700 bg-amber-50 p-3 text-sm text-amber-950">
            <p className="font-semibold">
              <span aria-hidden="true">! </span>
              {deployment.status === "applying" ? "Applying: the outcome is not known yet" : "Rolling back: the outcome is not known yet"}
            </p>
            <p>
              The intent was recorded before the CMS was called, and the CMS call is in flight or did not answer in time.
              The outcome is never guessed: reconciliation (<code>make deployments-reconcile</code>) resends the stored
              request, which the CMS answers with its recorded result, and then records it here. No action is available
              until then; reload later.
            </p>
          </div>
        )}
      </header>

      <Section id="facts" title="Deployment">
        <dl className={FACTS}>
          <Fact term="Source page">
            <Link href={pageHref(deployment.source_page_id)} className={`${LINK} break-all font-mono text-xs`}>
              {deployment.source_page_id}
            </Link>{" "}
            <ExternalUrl url={deployment.source_url} />
          </Fact>
          <Fact term="CMS">
            {deployment.cms_provider} · page <code className="break-all text-xs">{deployment.cms_page_id}</code>
          </Fact>
          <Fact term="Version before">
            <Version value={deployment.previous_version} none="Not recorded" />
          </Fact>
          <Fact term="Version after">
            <Version value={deployment.new_version} none="None (nothing applied yet)" />
          </Fact>
          <Fact term="Requested by">
            {actor.label}: {actor.id}
          </Fact>
          <Fact term="Requested">
            <When iso={deployment.created_at} />
          </Fact>
          <Fact term="Applied">
            <When iso={deployment.applied_at} none="Not applied" />
          </Fact>
          <Fact term="Verified">
            <When iso={deployment.verified_at} none="Not verified" />
          </Fact>
          <Fact term="Rolled back">
            <When iso={deployment.rolled_back_at} none="Not rolled back" />
          </Fact>
          <Fact term="Error">
            {deployment.error_code === null || deployment.error_code === undefined ? (
              <span className="text-slate-600">None</span>
            ) : (
              <>
                {describeDeploymentErrorCode(deployment.error_code)}{" "}
                <code className="text-xs text-slate-600">{deployment.error_code}</code>
              </>
            )}
          </Fact>
          <Fact term="Operation id">
            <code className="text-xs">{deployment.operation_id}</code>
          </Fact>
          <Fact term="Deployment id">
            <code className="text-xs">{deployment.id}</code>
          </Fact>
        </dl>
      </Section>

      {deployment.rollback_reason !== null && deployment.rollback_reason !== undefined && (
        <Section id="rollback-record" title="Rollback">
          <dl className={FACTS}>
            <Fact term="Reason">{describeRollbackReason(deployment.rollback_reason)}</Fact>
            <Fact term="Note">
              {deployment.rollback_note === null || deployment.rollback_note === undefined || deployment.rollback_note === "" ? (
                <span className="text-slate-600">None</span>
              ) : (
                <span className="whitespace-pre-wrap">{deployment.rollback_note}</span>
              )}
            </Fact>
          </dl>
        </Section>
      )}

      <Section id="actions" title="Actions">
        {!canDeploy ? (
          <p className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
            {user === null
              ? "Deployment actions are unavailable because your session could not be confirmed."
              : `Your role (${describeRole(user.role)}) can read deployments but not verify or roll them back.`}
          </p>
        ) : available.verify || available.rollback ? (
          <DeploymentActionsPanel
            deploymentId={deployment.id}
            available={available}
            rollbackable={rollbackablePlans(deployment)}
            actions={DEPLOYMENT_ACTIONS}
          />
        ) : (
          <p role="note" className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
            {inProgress
              ? "No action is available while the outcome is being reconciled."
              : `This deployment is ${status.label.toLowerCase()}; nothing of it is on the page to verify or roll back.`}
          </p>
        )}
      </Section>

      <Section id="items" title="Links">
        <TableFrame caption="Recommendations in this deployment" count={deployment.items.length}>
          <HeaderRow columns={["Anchor", "Recommendation status", "Target"]} />
          <tbody>
            {deployment.items.length === 0 ? (
              <EmptyRow colSpan={3}>No items were recorded.</EmptyRow>
            ) : (
              deployment.items.map((item) => (
                <tr key={item.plan_id} className={ROW}>
                  <th scope="row" className={`${CELL} font-medium`}>
                    <Link href={planHref(item.plan_id)} className={`${LINK} break-words`}>
                      {item.anchor_text}
                      <span className="sr-only"> — view recommendation</span>
                    </Link>
                  </th>
                  <td className={CELL}>
                    <StatusBadge presentation={describeLinkPlanStatus(item.plan_status)} />
                  </td>
                  <td className={CELL}>
                    <ExternalUrl url={item.target_url} />
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </TableFrame>
      </Section>

      <Section id="verification" title="Verification">
        <VerificationRuns runs={deployment.verifications} planLabels={planLabels} />
      </Section>

      <Section id="preview" title="Dry-run preview">
        <p className="max-w-prose text-sm text-slate-700">
          The change as validated before it was applied. Text only: the HTML snippets are shown as source and never
          rendered.
        </p>
        {deployment.preview.length === 0 ? (
          <p role="status" className="text-slate-700">
            No preview was recorded.
          </p>
        ) : (
          <div className="space-y-3">
            {previewsInOrder(deployment.preview, deployment.plan_ids).map((preview) => {
              const label = planLabels[preview.plan_id.toLowerCase()];
              return <ChangePreviewView key={preview.plan_id} preview={preview} {...(label === undefined ? {} : { label })} />;
            })}
          </div>
        )}
      </Section>
    </div>
  );
}
