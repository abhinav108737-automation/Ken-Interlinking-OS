import type { ScoreBreakdown } from "@/lib/api-client";
import { formatScore } from "@/lib/intelligence";
import { checkScore, describeScoreComponent, describeScoreFactor } from "@/lib/recommendations";
import type { StatusPresentation } from "@/lib/status";

import { StatusBadge } from "../StatusBadge";
import { CELL, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

const IDENTITIES_HOLD: StatusPresentation = {
  label: "Score adds up",
  description: "Relevance equals the sum of contributions, and the total equals relevance times the factors.",
  tone: "positive",
  known: true,
};

const IDENTITIES_FAIL: StatusPresentation = {
  label: "Score does not add up",
  description: "Recomputing the score from this record does not reproduce the stored relevance or total.",
  tone: "critical",
  known: true,
};

/**
 * The transparent score (ADR-0011 §4): weighted components, relevance, factors
 * and total, re-checked from the record alone.
 */
export function ScoreBreakdownTable({ score }: { readonly score: ScoreBreakdown }) {
  const check = checkScore(score);
  const factors = Object.entries(score.factors).sort(([a], [b]) => a.localeCompare(b, "en"));
  return (
    <div className="space-y-3">
      <TableFrame caption="Score breakdown">
        <thead className="bg-slate-100 text-slate-900">
          <tr>
            <th scope="col" className={`${CELL} font-semibold`}>
              Component
            </th>
            <th scope="col" className={`${NUM_CELL} font-semibold`}>
              Value
            </th>
            <th scope="col" className={`${NUM_CELL} font-semibold`}>
              Weight
            </th>
            <th scope="col" className={`${NUM_CELL} font-semibold`}>
              Contribution
            </th>
          </tr>
        </thead>
        <tbody>
          {score.components.map((component) => {
            const named = describeScoreComponent(component.name);
            return (
              <tr key={component.name} className={ROW}>
                <th scope="row" className={`${CELL} font-medium`}>
                  {named.label}{" "}
                  <span className="ml-1 font-mono text-xs font-normal text-slate-600">{component.name}</span>
                  {named.description !== null && (
                    <span className="block text-xs font-normal text-slate-700"> {named.description}</span>
                  )}
                </th>
                <td className={NUM_CELL}>{formatScore(component.value)}</td>
                <td className={NUM_CELL}>{formatScore(component.weight)}</td>
                <td className={NUM_CELL}>{formatScore(component.contribution)}</td>
              </tr>
            );
          })}
        </tbody>
        <tfoot className="border-t-2 border-slate-400">
          <tr className={ROW}>
            <th scope="row" colSpan={3} className={`${CELL} font-semibold`}>
              Relevance (sum of contributions)
            </th>
            <td className={`${NUM_CELL} font-semibold`}>{formatScore(score.relevance)}</td>
          </tr>
          {factors.map(([name, value]) => {
            const named = describeScoreFactor(name);
            return (
              <tr key={name} className={ROW}>
                <th scope="row" colSpan={3} className={`${CELL} font-medium`}>
                  × {named.label}{" "}
                  <span className="ml-1 font-mono text-xs font-normal text-slate-600">{name}</span>
                  {named.description !== null && (
                    <span className="block text-xs font-normal text-slate-700"> {named.description}</span>
                  )}
                </th>
                <td className={NUM_CELL}>{formatScore(value)}</td>
              </tr>
            );
          })}
          <tr className={ROW}>
            <th scope="row" colSpan={3} className={`${CELL} font-semibold`}>
              Total (relevance × factors)
            </th>
            <td className={`${NUM_CELL} font-semibold`}>{formatScore(score.total)}</td>
          </tr>
        </tfoot>
      </TableFrame>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
        <dt className="font-medium text-slate-700">Check</dt>
        <dd>
          <StatusBadge presentation={check.relevanceHolds && check.totalHolds ? IDENTITIES_HOLD : IDENTITIES_FAIL} />
          <span className="ml-2 text-xs text-slate-700">
            recomputed: sum {formatScore(check.sumOfContributions)}, factors {formatScore(check.productOfFactors)}
          </span>
        </dd>
        <dt className="font-medium text-slate-700">Semantic floor</dt>
        <dd>
          {score.semantic_floor === null ? (
            <span className="text-slate-600">None recorded</span>
          ) : (
            <span className="tabular-nums">{formatScore(score.semantic_floor)}</span>
          )}
        </dd>
        <dt className="font-medium text-slate-700">Ranking policy</dt>
        <dd>
          <code className="text-xs">{score.policy_name}</code> version {score.policy_version}
        </dd>
      </dl>
    </div>
  );
}
