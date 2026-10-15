import Link from "next/link";

import type { IssueView } from "@/lib/deployments";
import { planHref } from "@/lib/recommendations";

/** Deployment issues (fixed vocabulary), each with its plan when it concerns one. */
export function IssueList({
  issues,
  planLabels = {},
  label = "Deployment issues",
}: {
  readonly issues: readonly IssueView[];
  readonly planLabels?: Readonly<Record<string, string>>;
  readonly label?: string;
}) {
  if (issues.length === 0) return null;
  return (
    <ul aria-label={label} className="list-disc space-y-1 pl-5 text-sm">
      {issues.map((issue, index) => (
        <li key={`${issue.code}-${issue.planId ?? "request"}-${index}`}>
          <span className="font-medium">{issue.label}</span>
          {issue.description !== null && <span className="text-slate-800"> — {issue.description}</span>}{" "}
          <code className="text-xs text-slate-600">{issue.code}</code>
          {issue.planId !== null && (
            <>
              {" "}
              <span className="text-slate-700">(recommendation </span>
              <Link href={planHref(issue.planId)} className="break-all text-blue-800 underline underline-offset-2">
                {planLabels[issue.planId] === undefined ? issue.planId : `“${planLabels[issue.planId]}”`}
              </Link>
              <span className="text-slate-700">)</span>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}
