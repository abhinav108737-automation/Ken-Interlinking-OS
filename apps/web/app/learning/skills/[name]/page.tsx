import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { CorpusBanner } from "@/components/learning/CorpusBanner";
import { SkillTable } from "@/components/learning/SkillTable";
import { getLearningOverview, getSkill, isMissingResourceError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { SKILLS_PATH, describeSkillSource, describeSkillStatus } from "@/lib/learning";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Skill",
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

/**
 * One skill, with every version of it (ADR-0016 section 4).
 *
 * A skill is memory: a named, versioned instruction set. Promoting a new version supersedes
 * THAT name and nothing else, which is why the version list is the main content here -- what
 * is in force is one row of it, and the rest is the history that explains it.
 *
 * `instructions`, `input_schema` and `output_schema` can be model-written, so they are
 * rendered as text. Nothing on this page is ever injected as markup.
 */
export default async function SkillDetailPage({ params }: { readonly params: Promise<{ name: string }> }) {
  const { name } = await params;
  const [result, overview] = await Promise.all([getSkill(name), getLearningOverview()]);

  if (!result.ok) {
    if (isMissingResourceError(result.error)) {
      notFound();
    }
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Skill</h1>
        {overview.ok && <CorpusBanner banner={overview.data.banner} />}
        <ApiErrorNotice error={result.error} what="This skill" />
        <Link href={SKILLS_PATH} className="underline underline-offset-2">
          Back to skills
        </Link>
      </div>
    );
  }

  const skill = result.data;
  const status = describeSkillStatus(skill.status);

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <p className="text-sm">
          <Link href={SKILLS_PATH} className="underline underline-offset-2">
            Skills
          </Link>
        </p>
        <h1 className="text-2xl font-semibold">{skill.name}</h1>
        {overview.ok && <CorpusBanner banner={overview.data.banner} />}
        <p className="max-w-prose text-slate-800">
          {skill.description} Version {skill.version}, {status.label.toLowerCase()}, written by{" "}
          {describeSkillSource(skill.source)}, run in the {skill.model_role} model role.
          {skill.promoted_by === null
            ? " This version was never promoted, so it is not in use."
            : ` Promoted by ${skill.promoted_by}${
                skill.promoted_at === null ? "" : ` on ${formatDateTime(skill.promoted_at)}`
              }.`}
        </p>
      </header>

      <Section id="instructions-heading" title="Instructions">
        <p className="max-w-prose whitespace-pre-wrap rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-900">
          {skill.instructions}
        </p>
      </Section>

      <Section id="schemas-heading" title="Input and output schemas">
        <p className="max-w-prose text-sm text-slate-800">
          The model&apos;s output is validated against the output schema before it crosses any service boundary. A
          response that does not satisfy it is a fallback, never a result.
        </p>
        <dl className="space-y-2">
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-700">Input</dt>
          <dd>
            <pre className="overflow-x-auto rounded border border-slate-300 bg-slate-50 p-3 text-xs text-slate-900">
              {JSON.stringify(skill.input_schema, null, 2)}
            </pre>
          </dd>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-700">Output</dt>
          <dd>
            <pre className="overflow-x-auto rounded border border-slate-300 bg-slate-50 p-3 text-xs text-slate-900">
              {JSON.stringify(skill.output_schema, null, 2)}
            </pre>
          </dd>
        </dl>
      </Section>

      <Section id="versions-heading" title="Versions">
        <p className="max-w-prose text-sm text-slate-800">
          Promoting a version supersedes this name only. Every other skill, and every policy, is untouched by it.
        </p>
        <SkillTable
          skills={skill.versions}
          total={skill.versions.length}
          caption={`Every version of ${skill.name}`}
        />
      </Section>
    </div>
  );
}
