import Link from "next/link";

import {
  blockKindOf,
  blockReasonText,
  blockedCodeHref,
  describeBlockKind,
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
  reason = null,
  linkToFilter = false,
}: {
  readonly blocks: readonly string[];
  /** The recorded plain-language reason, rendered as text. */
  readonly reason?: string | null;
  /** Link each code to the blocked list filtered to it. */
  readonly linkToFilter?: boolean;
}) {
  const text = blockReasonText(reason);
  const reasonNote =
    text === null ? null : (
      <p className="max-w-prose break-words rounded border border-slate-400 bg-white p-3 text-sm text-slate-900">
        <span className="font-semibold">Recorded reason: </span>
        {text}
      </p>
    );
  if (blocks.length === 0) {
    const kind = describeBlockKind(blockKindOf(blocks));
    return (
      <div className="space-y-2">
        <p role="note" className="max-w-prose rounded border border-amber-700 bg-amber-50 p-3 text-sm text-amber-950">
          <span className="font-semibold">{kind.label}. </span>
          {kind.description} No deterministic block code fired, so this is not a safety refusal.
        </p>
        {reasonNote}
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {reasonNote}
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
    </div>
  );
}
