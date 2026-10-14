import type { LinkPlanDetail } from "@/lib/api-client";
import { codePointLength, splitHighlight } from "@/lib/recommendations";

/**
 * The anchor inside its text. Everything is a React text node — never HTML —
 * so markup in page text (`<script>`, `<img onerror>`) is displayed, not run.
 * The visual highlight is a `<mark>`; screen readers get explicit boundaries.
 */
export function HighlightedAnchor({
  before,
  anchor,
  after,
  leadingEllipsis = false,
  trailingEllipsis = false,
  linked = false,
}: {
  readonly before: string;
  readonly anchor: string;
  readonly after: string;
  readonly leadingEllipsis?: boolean;
  readonly trailingEllipsis?: boolean;
  /** Style the anchor as the proposed link (the "after" view). */
  readonly linked?: boolean;
}) {
  return (
    <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-900">
      {leadingEllipsis && "…"}
      {before}
      <span className="sr-only">[anchor start] </span>
      <mark
        className={`rounded-sm bg-yellow-200 px-0.5 font-semibold text-slate-950 ${linked ? "text-blue-900 underline decoration-2 underline-offset-2" : ""}`}
      >
        {anchor}
      </mark>
      <span className="sr-only"> [anchor end]</span>
      {after}
      {trailingEllipsis && "…"}
    </p>
  );
}

/**
 * Before/after view of the placement preview: the source text as it is now,
 * and the same text with the proposed link (highlighted). Read-only.
 */
export function PlacementPreviewPanel({ plan }: { readonly plan: LinkPlanDetail }) {
  const { preview, locator } = plan;
  const blockLength = codePointLength(plan.source_block_text);
  const leading = codePointLength(preview.before) < locator.start;
  const trailing = locator.end + codePointLength(preview.after) < blockLength;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <figure className="space-y-2 rounded border border-slate-300 bg-white p-3">
        <figcaption className="text-sm font-semibold text-slate-900">Before (current text)</figcaption>
        <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-900">
          {leading && "…"}
          {preview.before}
          {preview.anchor}
          {preview.after}
          {trailing && "…"}
        </p>
      </figure>
      <figure className="space-y-2 rounded border border-slate-300 bg-white p-3">
        <figcaption className="text-sm font-semibold text-slate-900">After (with the proposed link)</figcaption>
        <HighlightedAnchor
          before={preview.before}
          anchor={preview.anchor}
          after={preview.after}
          leadingEllipsis={leading}
          trailingEllipsis={trailing}
          linked
        />
      </figure>
    </div>
  );
}

/**
 * The full source block text, with the anchor highlighted at the locator's
 * offsets — but only if those offsets select the anchor text verbatim.
 */
export function SourceBlockText({ plan }: { readonly plan: LinkPlanDetail }) {
  const parts = splitHighlight(plan.source_block_text, plan.locator.start, plan.locator.end, plan.anchor_text);
  return (
    <div className="space-y-2">
      <section
        aria-label="Source block text"
        tabIndex={0}
        className="max-h-96 overflow-auto rounded border border-slate-300 bg-slate-50 p-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700"
      >
        {parts === null ? (
          <p className="whitespace-pre-wrap break-words text-sm text-slate-900">{plan.source_block_text}</p>
        ) : (
          <HighlightedAnchor before={parts.before} anchor={parts.match} after={parts.after} />
        )}
      </section>
      {parts === null && (
        <p role="note" className="rounded border border-amber-700 bg-amber-50 p-2 text-sm text-amber-950">
          <span aria-hidden="true">! </span>
          The locator offsets ({plan.locator.start}–{plan.locator.end}) do not select the anchor text in this block
          text, so nothing is highlighted. Phase 6 locates the block by context hash, anchor text and occurrence, not by
          these offsets alone.
        </p>
      )}
    </div>
  );
}
