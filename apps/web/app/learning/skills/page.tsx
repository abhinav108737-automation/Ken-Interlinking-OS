import type { Metadata } from "next";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { CorpusBanner } from "@/components/learning/CorpusBanner";
import { SkillTable } from "@/components/learning/SkillTable";
import { FilterForm, SelectField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { getLearningOverview, listSkills } from "@/lib/api";
import { DEFAULT_PAGE_SIZE, type SearchParams, parseLimit, parseOffset } from "@/lib/inventory";
import { SKILLS_PATH, SKILL_STATUSES, describeSkillStatus, parseSkillStatus } from "@/lib/learning";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Skill versions",
};

const STATUS_OPTIONS = SKILL_STATUSES.map((value) => ({ value, label: describeSkillStatus(value).label }));

export default async function SkillsPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const status = parseSkillStatus(params);
  const limit = parseLimit(params);
  const offset = parseOffset(params);

  const [result, overview] = await Promise.all([
    listSkills({ status: status ?? null, limit, offset }),
    getLearningOverview(),
  ]);

  const state = { status, limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit };

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Skill versions</h1>
        {overview.ok && <CorpusBanner banner={overview.data.banner} />}
        <p className="max-w-prose text-slate-800">
          Every version of every skill, promoted or not. A skill is in use only once a human promoted it with a
          reference to a passing evaluation run; an agent can propose one but can never promote it.
        </p>
      </header>

      <section aria-labelledby="skills-heading" className="space-y-3">
        <h2 id="skills-heading" className="text-xl font-semibold">
          Versions
        </h2>
        <FilterForm
          action={SKILLS_PATH}
          label="Filter skill versions"
          {...(status === undefined ? {} : { clearHref: SKILLS_PATH })}
        >
          <SelectField id="skills-status" name="status" label="Status" value={status ?? ""} options={STATUS_OPTIONS} />
        </FilterForm>
        {result.ok ? (
          <>
            <SkillTable skills={result.data.items} total={result.data.page.total} />
            <Pagination
              page={result.data.page}
              itemCount={result.data.items.length}
              pathname={SKILLS_PATH}
              params={state}
              label="Skill versions pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={result.error} what="Skill versions" />
        )}
      </section>
    </div>
  );
}
