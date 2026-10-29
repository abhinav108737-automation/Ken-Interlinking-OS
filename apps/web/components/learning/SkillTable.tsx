import Link from "next/link";

import type { SkillSummary } from "@/lib/api-client";
import { formatDateTime } from "@/lib/format";
import { formatScore } from "@/lib/intelligence";
import { describeSkillSource, describeSkillStatus, skillHref } from "@/lib/learning";
import { describeActor } from "@/lib/recommendations";

import { StatusBadge } from "../StatusBadge";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Status", "Skill", "Version", "Source", "Evaluation", "Promoted by", "Created"] as const;

/**
 * Skill versions. A skill is only in use once a HUMAN promoted it with an
 * evaluation reference; an agent can propose one but never promote it.
 *
 * `name` and `description` are model-influenced text and render as text.
 */
export function SkillTable({
  skills,
  total,
  caption = "Skill versions",
  linkToSkill = true,
}: {
  readonly skills: readonly SkillSummary[];
  readonly total: number;
  readonly caption?: string;
  readonly linkToSkill?: boolean;
}) {
  return (
    <TableFrame caption={caption} count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {skills.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>No skill version recorded.</EmptyRow>
        ) : (
          skills.map((skill) => {
            const promoter = skill.promoted_by === null ? null : describeActor(skill.promoted_by);
            return (
              <tr key={skill.id} className={ROW}>
                <th scope="row" className={CELL}>
                  <StatusBadge presentation={describeSkillStatus(skill.status)} />
                </th>
                <td className={`${CELL} max-w-sm break-words`}>
                  {linkToSkill ? (
                    <Link href={skillHref(skill.name)} className="text-blue-800 underline underline-offset-2">
                      {skill.name}
                    </Link>
                  ) : (
                    <span className="font-medium">{skill.name}</span>
                  )}
                  <span className="block text-xs text-slate-700">{skill.description}</span>
                </td>
                <td className={NUM_CELL}>{skill.version}</td>
                <td className={CELL}>{describeSkillSource(skill.source)}</td>
                <td className={CELL}>
                  {skill.evaluation_score === null ? (
                    <span className="text-slate-600">Not evaluated</span>
                  ) : (
                    <span className="tabular-nums">{formatScore(skill.evaluation_score)}</span>
                  )}
                  {skill.evaluation_run_id !== null && (
                    <span className="block break-all font-mono text-xs text-slate-700">
                      {skill.evaluation_run_id}
                    </span>
                  )}
                </td>
                <td className={CELL}>
                  {promoter === null ? (
                    <span className="text-slate-600">Not promoted</span>
                  ) : (
                    <>
                      <span className="whitespace-nowrap">{promoter.label}</span>
                      <span className="block break-all font-mono text-xs text-slate-700">{promoter.id}</span>
                      {skill.promoted_at !== null && (
                        <time dateTime={skill.promoted_at} className="block text-xs text-slate-700">
                          {formatDateTime(skill.promoted_at)}
                        </time>
                      )}
                    </>
                  )}
                </td>
                <td className={CELL}>
                  <time dateTime={skill.created_at} className="whitespace-nowrap text-xs">
                    {formatDateTime(skill.created_at)}
                  </time>
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableFrame>
  );
}
