import Link from "next/link";
import type { ReactNode } from "react";

import { ExternalUrl } from "@/components/inventory/ExternalUrl";
import { pageHref } from "@/components/inventory/PageInventoryTable";
import type { LinkAttribution } from "@/lib/api-client";
import { pageImpactHref } from "@/lib/analytics";
import { NONE, formatDateTime } from "@/lib/format";
import { deploymentHref } from "@/lib/deployments";
import { describeRelationshipType, jobRunHref } from "@/lib/intelligence";
import { describeActor, describeReasonCode, planHref } from "@/lib/recommendations";

const LINK = "text-blue-800 underline underline-offset-2";

function Step({ term, children }: { readonly term: string; readonly children: ReactNode }) {
  return (
    <li className="space-y-0.5">
      <p className="text-sm font-medium text-slate-700">{term}</p>
      <div className="text-sm text-slate-900">{children}</div>
    </li>
  );
}

function Missing({ children }: { readonly children: ReactNode }) {
  return <span className="text-slate-600">{children}</span>;
}

function When({ iso, none }: { readonly iso: string | null; readonly none: string }) {
  return iso === null ? <Missing>{none}</Missing> : <time dateTime={iso}>{formatDateTime(iso)}</time>;
}

/**
 * Recommendation run → agent → human → deployment → rollback, as the API
 * computed it from the immutable plan (ADR-0015 section 3). Nothing here is
 * recomputed: every value is the API's own, and an absent step says so.
 */
export function AttributionChain({ attribution }: { readonly attribution: LinkAttribution }) {
  const approver = attribution.approved_by === null ? null : describeActor(attribution.approved_by);
  return (
    <div className="space-y-4">
      <ol aria-label="Attribution chain" className="grid gap-3 sm:grid-cols-2">
        <Step term="Recommendation run">
          {attribution.recommendation_run_id === null ? (
            <Missing>Not recorded</Missing>
          ) : (
            <Link href={jobRunHref(attribution.recommendation_run_id)} className={`${LINK} font-mono text-xs`}>
              {attribution.recommendation_run_id}
            </Link>
          )}
        </Step>
        <Step term="Opportunity">
          {attribution.reason_codes.length === 0 ? (
            <Missing>No reason code recorded</Missing>
          ) : (
            attribution.reason_codes.map(describeReasonCode).join(", ")
          )}
        </Step>
        <Step term="Agent review">
          {attribution.agent_run_id === null ? (
            <Missing>No agent reviewed this link</Missing>
          ) : (
            <>
              {attribution.agent_verdict ?? NONE}
              {attribution.agent_model !== null && <span className="text-slate-700"> · model {attribution.agent_model}</span>}
              {attribution.agent_graph_version !== null && (
                <span className="text-slate-700"> · graph {attribution.agent_graph_version}</span>
              )}
            </>
          )}
        </Step>
        <Step term="Human approval">
          {approver === null ? (
            <Missing>No approval recorded</Missing>
          ) : (
            <>
              {approver.label}: {approver.id} · <When iso={attribution.approved_at} none="time not recorded" />
            </>
          )}
        </Step>
        <Step term="Deployment">
          {attribution.deployment_id === null ? (
            <Missing>Never deployed</Missing>
          ) : (
            <>
              <Link href={deploymentHref(attribution.deployment_id)} className={`${LINK} font-mono text-xs`}>
                {attribution.deployment_id}
              </Link>
              {attribution.cms_provider !== null && <span className="text-slate-700"> · {attribution.cms_provider}</span>}
            </>
          )}
        </Step>
        <Step term="On the page">
          <When iso={attribution.deployed_at} none="Never on a page" />
          {attribution.verified_at !== null && (
            <span className="text-slate-700">
              {" "}
              · verified <time dateTime={attribution.verified_at}>{formatDateTime(attribution.verified_at)}</time>
            </span>
          )}
        </Step>
        <Step term="Rolled back">
          <When iso={attribution.rolled_back_at} none="Not rolled back" />
        </Step>
        <Step term="Supersedes">
          {attribution.supersedes === null ? (
            <Missing>Nothing (this is the original recommendation)</Missing>
          ) : (
            <Link href={planHref(attribution.supersedes)} className={`${LINK} font-mono text-xs`}>
              {attribution.supersedes}
            </Link>
          )}
        </Step>
      </ol>

      <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
        <dt className="font-medium text-slate-700">Source page</dt>
        <dd className="min-w-0 break-words">
          <Link href={pageHref(attribution.source_page_id)} className={LINK}>
            Inventory
          </Link>{" "}
          ·{" "}
          <Link href={pageImpactHref(attribution.source_page_id)} className={LINK}>
            Page impact
          </Link>{" "}
          · v{attribution.source_page_version} · <ExternalUrl url={attribution.source_url} />
        </dd>
        <dt className="font-medium text-slate-700">Target page</dt>
        <dd className="min-w-0 break-words">
          <Link href={pageHref(attribution.target_page_id)} className={LINK}>
            Inventory
          </Link>{" "}
          ·{" "}
          <Link href={pageImpactHref(attribution.target_page_id)} className={LINK}>
            Page impact
          </Link>{" "}
          · v{attribution.target_page_version} · <ExternalUrl url={attribution.target_url} />
        </dd>
        <dt className="font-medium text-slate-700">Relationship</dt>
        <dd>
          {attribution.relationship_type === null ? (
            <Missing>None (no typed relationship)</Missing>
          ) : (
            describeRelationshipType(attribution.relationship_type)
          )}
        </dd>
        <dt className="font-medium text-slate-700">Anchor</dt>
        <dd className="break-words">
          “{attribution.anchor_text}” <span className="text-slate-700">({attribution.anchor_source})</span>
        </dd>
        <dt className="font-medium text-slate-700">Versions</dt>
        <dd className="break-words">
          engine <code className="text-xs">{attribution.engine_version}</code> · policy {attribution.policy_version} ·
          ranking {attribution.ranking_policy_version} · analysis{" "}
          <code className="text-xs">{attribution.analysis_version}</code> · calibration{" "}
          {attribution.calibration_version === null ? (
            <Missing>none</Missing>
          ) : (
            <code className="text-xs">{attribution.calibration_version}</code>
          )}
        </dd>
      </dl>
    </div>
  );
}
