import Link from "next/link";

import {
  blockedCodeHref,
  describeBlockCode,
  describeBlockCodeDetail,
  describeBlockInvestigation,
  isKnownBlockCode,
} from "@/lib/autonomy";

/**
 * The deterministic blocks that fired, each with what it means and what to
 * check. This is the investigation surface: a code on its own tells an operator
 * nothing, so the list always carries the explanation and the next step.
 *
 * Codes are contract enum values and are rendered as text; a code this build
 * does not know is shown and flagged rather than hidden, because the API
 * enforces it either way.
 */
export function BlockList({
  blocks,
  linkToFilter = false,
}: {
  readonly blocks: readonly string[];
  /** Link each code to the blocked list filtered to it. */
  readonly linkToFilter?: boolean;
}) {
  if (blocks.length === 0) {
    return (
      <p role="note" className="rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
        The API reported this plan as blocked but recorded no block code. Treat it as unexplained and investigate.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {blocks.map((code) => {
        const known = isKnownBlockCode(code);
        const investigate = describeBlockInvestigation(code);
        return (
          <li key={code} className="rounded border border-red-700 bg-red-50 p-3 text-sm text-red-950">
            <p className="font-semibold">
              <span aria-hidden="true">✕ </span>
              {linkToFilter && known ? (
                <Link href={blockedCodeHref(code)} className="underline underline-offset-2">
                  {describeBlockCode(code)}
                </Link>
              ) : (
                describeBlockCode(code)
              )}
              <span className="ml-2 font-mono text-xs font-normal">{code}</span>
            </p>
            <p className="max-w-prose">{describeBlockCodeDetail(code)}</p>
            {investigate !== null && (
              <p className="mt-1 max-w-prose">
                <span className="font-medium">What to check: </span>
                {investigate}
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
}
