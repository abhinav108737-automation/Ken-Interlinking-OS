import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { CorpusBanner } from "@/components/learning/CorpusBanner";
import { EvaluationTable } from "@/components/learning/EvaluationTable";
import { ProposalTable } from "@/components/learning/ProposalTable";
import { SkillTable } from "@/components/learning/SkillTable";
import { CountTable } from "@/components/intelligence/CountTable";
import {
  getLearningOverview,
  listLearningEvaluations,
  listLearningProposals,
  listSkills,
} from "@/lib/api";
import { buildHref } from "@/lib/inventory";
import { jobRunHref, sortCounts } from "@/lib/intelligence";
import {
  EVALUATIONS_PATH,
  PROPOSALS_PATH,
  SKILLS_PATH,
  describeGateResult,
  describeProposalStatus,
  describeSkillStatus,
} from "@/lib/learning";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Learning",
};

const RECENT = 5;

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

export default async function LearningPage() {
  const [overview, proposals, skills, evaluations] = await Promise.all([
    getLearningOverview(),
    listLearningProposals({ limit: RECENT }),
    listSkills({ limit: RECENT }),
    listLearningEvaluations({ limit: RECENT }),
  ]);

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Learning</h1>
        {overview.ok ? (
          <CorpusBanner banner={overview.data.banner} />
        ) : (
          <ApiErrorNotice error={overview.error} what="Learning overview" />
        )}
        <p className="max-w-prose text-slate-800">
          Recorded experience becomes structured feedback, then a proposal, then an offline evaluation, then a shadow or
          canary run — and only then a human promotion. An agent may propose a policy or a skill; it may never promote
          one, and it may never override a deterministic filter.
        </p>
      </header>

      {overview.ok && (
        <>
          <Section id="counts" title="Where things stand">
            <div className="grid gap-4 lg:grid-cols-2">
              <CountTable
                caption="Proposals by status"
                keyHeader="Status"
                valueHeader="Proposals"
                rows={sortCounts(overview.data.proposals_by_status).map(({ key, count }) => ({
                  key,
                  label: describeProposalStatus(key).label,
                  count,
                  href: buildHref(PROPOSALS_PATH, { status: key }),
                }))}
                emptyMessage="No proposal yet."
              />
              <CountTable
                caption="Skill versions by status"
                keyHeader="Status"
                valueHeader="Skill versions"
                rows={sortCounts(overview.data.skills_by_status).map(({ key, count }) => ({
                  key,
                  label: describeSkillStatus(key).label,
                  count,
                  href: buildHref(SKILLS_PATH, { status: key }),
                }))}
                emptyMessage="No skill version yet."
              />
              <CountTable
                caption="Evaluation runs by gate result"
                keyHeader="Gate result"
                valueHeader="Runs"
                rows={sortCounts(overview.data.evaluations_by_gate_result).map(({ key, count }) => ({
                  key,
                  label: describeGateResult(key).label,
                  count,
                  href: buildHref(EVALUATIONS_PATH, { gate_result: key }),
                }))}
                emptyMessage="No evaluation run yet."
              />
              <CountTable
                caption="Experiments by status"
                keyHeader="Status"
                valueHeader="Experiments"
                rows={sortCounts(overview.data.experiments_by_status).map(({ key, count }) => ({
                  key,
                  label: key,
                  count,
                }))}
                emptyMessage="No experiment yet."
              />
            </div>
            <dl className="flex flex-wrap gap-x-8 gap-y-1 text-sm">
              <div className="flex gap-2">
                <dt className="font-medium text-slate-700">Learning policy</dt>
                <dd>version {overview.data.policy_version}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="font-medium text-slate-700">Latest reflection run</dt>
                <dd>
                  {overview.data.latest_reflection_run_id === null ? (
                    <span className="text-slate-600">none yet</span>
                  ) : (
                    <Link
                      href={jobRunHref(overview.data.latest_reflection_run_id)}
                      className="font-mono text-xs text-blue-800 underline"
                    >
                      {overview.data.latest_reflection_run_id}
                    </Link>
                  )}
                </dd>
              </div>
              <div className="flex gap-2">
                <dt className="font-medium text-slate-700">Latest evaluation run</dt>
                <dd>
                  {overview.data.latest_evaluation_run_id === null ? (
                    <span className="text-slate-600">none yet</span>
                  ) : (
                    <Link
                      href={jobRunHref(overview.data.latest_evaluation_run_id)}
                      className="font-mono text-xs text-blue-800 underline"
                    >
                      {overview.data.latest_evaluation_run_id}
                    </Link>
                  )}
                </dd>
              </div>
            </dl>
          </Section>
        </>
      )}

      <Section id="proposals" title="Latest proposals">
        {proposals.ok ? (
          <>
            <ProposalTable
              proposals={proposals.data.items}
              total={proposals.data.page.total}
              caption={`Latest ${RECENT} proposals`}
            />
            <p className="text-sm">
              <Link href={PROPOSALS_PATH} className="text-blue-800 underline underline-offset-4">
                All proposals
              </Link>
            </p>
          </>
        ) : (
          <ApiErrorNotice error={proposals.error} what="Proposals" />
        )}
      </Section>

      <Section id="skills" title="Skill versions">
        {skills.ok ? (
          <>
            <SkillTable
              skills={skills.data.items}
              total={skills.data.page.total}
              caption={`Latest ${RECENT} skill versions`}
            />
            <p className="text-sm">
              <Link href={SKILLS_PATH} className="text-blue-800 underline underline-offset-4">
                All skill versions
              </Link>
            </p>
          </>
        ) : (
          <ApiErrorNotice error={skills.error} what="Skill versions" />
        )}
      </Section>

      <Section id="evaluations" title="Evaluation outcomes">
        {evaluations.ok ? (
          <>
            <EvaluationTable
              evaluations={evaluations.data.items}
              total={evaluations.data.page.total}
              caption={`Latest ${RECENT} evaluation runs`}
            />
            <p className="text-sm">
              <Link href={EVALUATIONS_PATH} className="text-blue-800 underline underline-offset-4">
                All evaluation runs
              </Link>
            </p>
          </>
        ) : (
          <ApiErrorNotice error={evaluations.error} what="Evaluation runs" />
        )}
      </Section>
    </div>
  );
}
