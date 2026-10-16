import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { CorpusBanner } from "@/components/learning/CorpusBanner";
import { SkillTable } from "@/components/learning/SkillTable";
import { getLearningOverview, getSkill, isMissingResourceError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { formatScore } from "@/lib/intelligence";
import {
  SKILLS_PATH,
  describeEvalSuite,
  describeSkillSource,
  describeSkillStatus,
  proposalHref,
} from "@/lib/learning";
import { describeActor } from "@/lib/recommendations";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Skill",
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

export default async function SkillDetailPage({ params }: { readonly params: Promise<{ name: string }> }) {
  const { name } = await params;
  const [result, overview] = await Promise.all([getSkill(name), getLearningOverview()]);

  if (!result.ok) {
    if (isMissingResourceError(result.error)) notFound();
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Skill</h1>
        <ApiErrorNotice error={result.error} what="Skill" />
        <p>
          <Link href={SKILLS_PATH} className="text-blue-800 underline underline-offset-4">
            Back to skill versions
          </Link>
        </p>
      </div>
    );
  }

  const skill = result.data;
  const status = describeSkillStatus(skill.status);
  const promoter = skill.promoted_by === null ? null : describeActor(skill.promoted_by);
  const proposal = skill.proposal_id === null ? null : proposalHref(skill.proposal_id);

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <p className="text-sm">
          <Link href={SKILLS_PATH} className="text-blue-800 underline underline-offset-4">
            ← Skill versions
          </Link>
        </p>
        <h1 className="break-words text-2xl font-semibold">{skill.name}</h1>
        {overview.ok && <CorpusBanner banner={overview.data.banner} />}
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge presentation={status} />
          <span className="text-sm text-slate-700">version {skill.version}</span>
        </div>
        <p className="max-w-prose text-sm text-slate-800">{status.description}</p>
        <p className="max-w-prose break-words">{skill.description}</p>
      </header>

      <Section id="instructions" title="Instructions">
        <p className="text-sm text-slate-700">
          The skill&apos;s own text, shown verbatim as text. It is never rendered as markup and never executed here.
        </p>
        <pre className="max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded border border-slate-300 bg-slate-50 p-3 text-sm">
          {skill.instructions}
        </pre>
      </Section>

      <Section id="contract" title="Input and output contract">
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="space-y-1">
            <h3 className="font-semibold">Input schema</h3>
            <pre className="max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded border border-slate-300 bg-slate-50 p-3 text-xs">
              {JSON.stringify(skill.input_schema, null, 2)}
            </pre>
          </div>
          <div className="space-y-1">
            <h3 className="font-semibold">Output schema</h3>
            <pre className="max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded border border-slate-300 bg-slate-50 p-3 text-xs">
              {JSON.stringify(skill.output_schema, null, 2)}
            </pre>
          </div>
        </div>
      </Section>

      <Section id="facts" title="This version">
        <dl className={FACTS}>
          <Fact term="Model role">{skill.model_role}</Fact>
          <Fact term="Source">{describeSkillSource(skill.source)}</Fact>
          <Fact term="Evaluation suites">
            {skill.suites.length === 0 ? (
              <span className="text-slate-600">none</span>
            ) : (
              skill.suites.map(describeEvalSuite).join(", ")
            )}
          </Fact>
          <Fact term="Evaluation score">
            {skill.evaluation_score === null ? (
              <span className="text-slate-600">Not evaluated</span>
            ) : (
              <span className="tabular-nums">{formatScore(skill.evaluation_score)}</span>
            )}
          </Fact>
          <Fact term="Evaluation run">
            {skill.evaluation_run_id === null ? (
              <span className="text-slate-600">—</span>
            ) : (
              <code className="break-all text-xs">{skill.evaluation_run_id}</code>
            )}
          </Fact>
          <Fact term="From proposal">
            {proposal === null ? (
              <span className="text-slate-600">None (authored directly)</span>
            ) : (
              <Link href={proposal} className="font-mono text-xs text-blue-800 underline">
                {skill.proposal_id}
              </Link>
            )}
          </Fact>
          <Fact term="Promoted by">
            {promoter === null ? (
              <span className="text-slate-600">Not promoted — this version is not in use</span>
            ) : (
              <>
                <span className="whitespace-nowrap">{promoter.label}</span>{" "}
                <code className="break-all text-xs">{promoter.id}</code>
              </>
            )}
          </Fact>
          <Fact term="Promoted at">
            {skill.promoted_at === null ? (
              <span className="text-slate-600">—</span>
            ) : (
              <time dateTime={skill.promoted_at}>{formatDateTime(skill.promoted_at)}</time>
            )}
          </Fact>
          <Fact term="Created">
            <time dateTime={skill.created_at}>{formatDateTime(skill.created_at)}</time>
          </Fact>
        </dl>
      </Section>

      <Section id="versions" title="Version history">
        <SkillTable
          skills={skill.versions}
          total={skill.versions.length}
          caption={`Every version of ${skill.name}`}
          linkToSkill={false}
        />
      </Section>
    </div>
  );
}
