import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { CELL, HeaderRow, ROW, TableFrame } from "@/components/inventory/Table";
import { CorpusBanner } from "@/components/learning/CorpusBanner";
import { EvaluationTable } from "@/components/learning/EvaluationTable";
import { PromotionPanel } from "@/components/learning/PromotionPanel";
import { getLearningOverview, getLearningProposal, isMissingResourceError, requireSessionUser } from "@/lib/api";
import { can, permissionsOf } from "@/lib/auth";
import { formatDateTime } from "@/lib/format";
import {
  PROPOSALS_PATH,
  describeProposalKind,
  describeProposalStatus,
  describeTargetKind,
} from "@/lib/learning";
import { isUuid } from "@/lib/recommendations";

import { advanceAction } from "../../actions";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Learning proposal",
};

function Section({
  id,
  title,
  children,
}: {
  readonly id: string;
  readonly title: string;
  readonly children: ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="space-y-3">
      <h2 id={id} className="text-xl font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

/** A proposed value, rendered as JSON text. Never as markup: a proposal can be model-written. */
function Value({ value }: { readonly value: unknown }) {
  return <code className="break-all text-xs">{JSON.stringify(value)}</code>;
}

/**
 * One learning proposal (ADR-0016 sections 3 and 5).
 *
 * A proposal changes nothing by existing. This page shows what it would change, the evidence
 * it was built from, the evaluations run against it, and the one control that can move it --
 * which needs the `promote` permission and is still refused by a mechanical rule when the
 * corpus is demonstration data. Both the refusal and the control are shown: hiding the control
 * would make a deliberate rule look like a missing feature.
 *
 * Model-written text (title, rationale, changed values) is rendered as text, never as markup.
 */
export default async function ProposalDetailPage({
  params,
}: {
  readonly params: Promise<{ proposalId: string }>;
}) {
  const { proposalId } = await params;
  // A malformed id is a 404 before any call, so an id from the URL never reaches the API.
  if (!isUuid(proposalId)) {
    notFound();
  }

  const [result, overview, me] = await Promise.all([
    getLearningProposal(proposalId),
    getLearningOverview(),
    requireSessionUser(),
  ]);

  if (!result.ok) {
    if (isMissingResourceError(result.error)) {
      notFound();
    }
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Learning proposal</h1>
        {overview.ok && <CorpusBanner banner={overview.data.banner} />}
        <ApiErrorNotice error={result.error} what="This proposal" />
        <Link href={PROPOSALS_PATH} className="underline underline-offset-2">
          Back to proposals
        </Link>
      </div>
    );
  }

  const proposal = result.data;
  const canPromote = can(permissionsOf(me.ok ? me.data : null), "promote");
  const status = describeProposalStatus(proposal.status);

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <p className="text-sm">
          <Link href={PROPOSALS_PATH} className="underline underline-offset-2">
            Learning proposals
          </Link>
        </p>
        <h1 className="text-2xl font-semibold">{proposal.title}</h1>
        {overview.ok && <CorpusBanner banner={overview.data.banner} />}
        <p className="max-w-prose text-slate-800">
          {describeProposalKind(proposal.kind)} proposal
          {proposal.target_kind === null ? "" : ` for ${describeTargetKind(proposal.target_kind)}`}, against{" "}
          <code className="text-xs">{proposal.base_version}</code>. Status: {status.label}. Proposed by{" "}
          <span className="font-mono text-xs">{proposal.proposed_by}</span>
          {proposal.created_at === null ? "" : ` on ${formatDateTime(proposal.created_at)}`}.
        </p>
        {!proposal.frozen_fields_untouched && (
          <p role="alert" className="max-w-prose rounded border-2 border-red-800 bg-red-50 p-3 text-sm text-red-950">
            <span className="font-semibold">This proposal touches a frozen field.</span> A proposal may never change a
            frozen policy path, and the contract refuses to build one that does. A backend reporting this is wrong or
            newer than this console: do not promote it, and report it.
          </p>
        )}
      </header>

      <Section id="rationale-heading" title="Rationale">
        <p className="max-w-prose text-slate-900">{proposal.rationale}</p>
      </Section>

      <Section id="changes-heading" title="What this would change">
        <TableFrame caption="Changes to the policy document" count={proposal.changes.length}>
          <HeaderRow columns={["Path", "From", "To"]} />
          <tbody>
            {proposal.changes.map((change) => (
              <tr key={change.path} className={ROW}>
                <td className={CELL}>
                  <code className="break-all text-xs">{change.path}</code>
                </td>
                <td className={CELL}>
                  <Value value={change.from_value} />
                </td>
                <td className={CELL}>
                  <Value value={change.to_value} />
                </td>
              </tr>
            ))}
          </tbody>
        </TableFrame>
      </Section>

      <Section id="evaluations-heading" title="Evaluations">
        <p className="max-w-prose text-sm text-slate-800">
          An inconclusive gate is never a pass: it means the run could not decide, and a proposal that has not passed
          cannot be promoted.
        </p>
        <EvaluationTable
          evaluations={proposal.evaluations}
          total={proposal.evaluations.length}
          caption="Evaluation runs for this proposal"
        />
      </Section>

      <Section id="stages-heading" title="Required stages">
        <p className="max-w-prose text-sm text-slate-800">
          Completed: {proposal.stages_completed.length === 0 ? "none" : proposal.stages_completed.join(", ")}. Required:{" "}
          {proposal.required_stages.join(", ")}.
        </p>
      </Section>

      {canPromote ? (
        <PromotionPanel
          proposalId={proposalId}
          currentStatus={proposal.status}
          promotion={proposal.promotion}
          action={advanceAction}
        />
      ) : (
        <p className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
          Advancing or promoting a proposal needs the promote permission, which your role does not have. You can still
          read everything on this page.
        </p>
      )}
    </div>
  );
}
