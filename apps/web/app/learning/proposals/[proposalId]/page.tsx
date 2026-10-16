import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { CorpusBanner } from "@/components/learning/CorpusBanner";
import { EvaluationTable } from "@/components/learning/EvaluationTable";
import { PromotionPanel } from "@/components/learning/PromotionPanel";
import { SkillTable } from "@/components/learning/SkillTable";
import { CELL, NUM_CELL, ROW, TableFrame } from "@/components/inventory/Table";
import { getLearningOverview, getLearningProposal, isMissingResourceError, requireSessionUser } from "@/lib/api";
import { can, describeRole, permissionsOf } from "@/lib/auth";
import { formatConfigValue, formatDateTime, shortHash } from "@/lib/format";
import {
  PROPOSALS_PATH,
  describeProposalKind,
  describeProposalRejectionReason,
  describeProposalStatus,
  describeTargetKind,
} from "@/lib/learning";
import { describeActor, isUuid } from "@/lib/recommendations";

import { advanceAction } from "../../actions";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Learning proposal",
};

const FACTS = "grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm";

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

export default async function ProposalDetailPage({
  params,
}: {
  readonly params: Promise<{ proposalId: string }>;
}) {
  const { proposalId } = await params;
  if (!isUuid(proposalId)) notFound();

  const [result, overview, me] = await Promise.all([
    getLearningProposal(proposalId),
    getLearningOverview(),
    requireSessionUser(),
  ]);

  if (!result.ok) {
    if (isMissingResourceError(result.error)) notFound();
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Learning proposal</h1>
        <ApiErrorNotice error={result.error} what="Proposal" />
        <p>
          <Link href={PROPOSALS_PATH} className="text-blue-800 underline underline-offset-4">
            Back to proposals
          </Link>
        </p>
      </div>
    );
  }

  const proposal = result.data;
  const status = describeProposalStatus(proposal.status);
  const actor = describeActor(proposal.proposed_by);
  const user = me.ok ? me.data : null;
  const canPromote = can(permissionsOf(user), "promote");

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <p className="text-sm">
          <Link href={PROPOSALS_PATH} className="text-blue-800 underline underline-offset-4">
            ← Proposals
          </Link>
        </p>
        <h1 className="break-words text-2xl font-semibold">{proposal.title}</h1>
        {overview.ok && <CorpusBanner banner={overview.data.banner} />}
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge presentation={status} />
          <span className="text-sm text-slate-700">{describeProposalKind(proposal.kind)} proposal</span>
        </div>
        <p className="max-w-prose text-sm text-slate-800">{status.description}</p>
        {!proposal.frozen_fields_untouched && (
          <p role="alert" className="max-w-prose rounded border-2 border-red-700 bg-red-50 p-3 text-sm text-red-950">
            <span aria-hidden="true">✕ </span>
            <span className="font-semibold">This proposal touches a frozen field.</span> It cannot be promoted; a
            frozen field is not something learning may change.
          </p>
        )}
      </header>

      <Section id="rationale" title="Rationale">
        <p className="max-w-prose whitespace-pre-wrap break-words">{proposal.rationale}</p>
      </Section>

      <Section id="changes" title="Proposed changes">
        {proposal.changes.length === 0 ? (
          <p role="status" className="text-slate-700">
            No field changes recorded.
          </p>
        ) : (
          <TableFrame caption="Changes to the policy document" count={proposal.changes.length}>
            <thead className="bg-slate-100 text-slate-900">
              <tr>
                <th scope="col" className={`${CELL} font-semibold`}>
                  Path
                </th>
                <th scope="col" className={`${NUM_CELL} font-semibold`}>
                  From
                </th>
                <th scope="col" className={`${NUM_CELL} font-semibold`}>
                  To
                </th>
              </tr>
            </thead>
            <tbody>
              {proposal.changes.map((change) => (
                <tr key={change.path} className={ROW}>
                  <th scope="row" className={`${CELL} font-medium`}>
                    <code className="break-all text-xs">{change.path}</code>
                  </th>
                  <td className={NUM_CELL}>{formatConfigValue(change.from_value)}</td>
                  <td className={NUM_CELL}>{formatConfigValue(change.to_value)}</td>
                </tr>
              ))}
            </tbody>
          </TableFrame>
        )}
      </Section>

      <Section id="stages" title="Stages">
        <dl className={FACTS}>
          <Fact term="Required">
            {proposal.required_stages.length === 0 ? (
              <span className="text-slate-600">none</span>
            ) : (
              proposal.required_stages.map((stage) => describeProposalStatus(stage).label).join(" → ")
            )}
          </Fact>
          <Fact term="Completed">
            {proposal.stages_completed.length === 0 ? (
              <span className="text-slate-600">none yet</span>
            ) : (
              proposal.stages_completed.map((stage) => describeProposalStatus(stage).label).join(", ")
            )}
          </Fact>
        </dl>
      </Section>

      <Section id="evaluations" title="Evaluation outcomes">
        <EvaluationTable
          evaluations={proposal.evaluations}
          total={proposal.evaluations.length}
          caption="Evaluation runs for this proposal"
        />
      </Section>

      {proposal.skill !== null && (
        <Section id="skill" title="Proposed skill version">
          <SkillTable skills={[proposal.skill]} total={1} caption="The skill version this proposal would create" />
        </Section>
      )}

      <Section id="history" title="Transition history">
        <TableFrame caption="Status transitions" count={proposal.transitions.length}>
          <thead className="bg-slate-100 text-slate-900">
            <tr>
              <th scope="col" className={`${CELL} font-semibold`}>
                To
              </th>
              <th scope="col" className={`${CELL} font-semibold`}>
                From
              </th>
              <th scope="col" className={`${CELL} font-semibold`}>
                Actor
              </th>
              <th scope="col" className={`${CELL} font-semibold`}>
                Reason and note
              </th>
              <th scope="col" className={`${CELL} font-semibold`}>
                When
              </th>
            </tr>
          </thead>
          <tbody>
            {proposal.transitions.length === 0 ? (
              <tr className={ROW}>
                <td colSpan={5} className={`${CELL} text-slate-700`}>
                  No transition recorded.
                </td>
              </tr>
            ) : (
              proposal.transitions.map((transition) => {
                const who = describeActor(transition.actor);
                return (
                  <tr key={transition.id} className={ROW}>
                    <th scope="row" className={CELL}>
                      <StatusBadge presentation={describeProposalStatus(transition.to_status)} />
                    </th>
                    <td className={CELL}>{describeProposalStatus(transition.from_status).label}</td>
                    <td className={CELL}>
                      <span className="whitespace-nowrap">{who.label}</span>
                      <span className="block break-all font-mono text-xs text-slate-700">{who.id}</span>
                    </td>
                    <td className={`${CELL} max-w-sm break-words`}>
                      {transition.reason === null ? (
                        <span className="text-slate-600">—</span>
                      ) : (
                        describeProposalRejectionReason(transition.reason)
                      )}
                      {transition.note !== null && transition.note.trim() !== "" && (
                        <span className="mt-1 block whitespace-pre-wrap text-xs text-slate-800">
                          {transition.note}
                        </span>
                      )}
                    </td>
                    <td className={CELL}>
                      <time dateTime={transition.created_at} className="whitespace-nowrap text-xs">
                        {formatDateTime(transition.created_at)}
                      </time>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </TableFrame>
      </Section>

      {canPromote ? (
        <PromotionPanel
          proposalId={proposal.id}
          currentStatus={proposal.status}
          promotion={proposal.promotion}
          action={advanceAction}
        />
      ) : (
        <p className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
          {user === null
            ? "Advancing a proposal is unavailable because your session could not be confirmed."
            : `Advancing or promoting a proposal needs the promote permission. Your role (${describeRole(user.role)}) does not have it.`}
        </p>
      )}

      <Section id="provenance" title="Provenance">
        <dl className={FACTS}>
          <Fact term="Proposed by">
            <span className="whitespace-nowrap">{actor.label}</span>{" "}
            <code className="break-all text-xs">{actor.id}</code>
          </Fact>
          <Fact term="Model">
            {proposal.model === null ? (
              <span className="text-slate-600">None (deterministic)</span>
            ) : (
              <code className="text-xs">{proposal.model}</code>
            )}
          </Fact>
          <Fact term="Fallback used">{proposal.fallback_used ? "yes" : "no"}</Fact>
          <Fact term="Target">
            {proposal.target_kind === null ? (
              <span className="text-slate-600">—</span>
            ) : (
              describeTargetKind(proposal.target_kind)
            )}
          </Fact>
          <Fact term="Base version">
            <code className="text-xs">{proposal.base_version}</code>
          </Fact>
          <Fact term="Fingerprint">
            <code className="text-xs" title={proposal.fingerprint}>
              {shortHash(proposal.fingerprint)}
            </code>
          </Fact>
          <Fact term="Reflection run">
            <code className="break-all text-xs">{proposal.job_run_id}</code>
          </Fact>
          <Fact term="Created">
            <time dateTime={proposal.created_at}>{formatDateTime(proposal.created_at)}</time>
          </Fact>
          <Fact term="Updated">
            <time dateTime={proposal.updated_at}>{formatDateTime(proposal.updated_at)}</time>
          </Fact>
          <Fact term="Proposal id">
            <code className="break-all text-xs">{proposal.id}</code>
          </Fact>
        </dl>
      </Section>
    </div>
  );
}
