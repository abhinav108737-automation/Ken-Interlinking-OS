import Link from "next/link";

import type { ChangePreview } from "@/lib/api-client";
import { previewHighlight } from "@/lib/deployments";
import { planHref } from "@/lib/recommendations";

import { HighlightedAnchor } from "../recommendations/PlacementPreview";

const LINK = "text-blue-800 underline underline-offset-2";
const PRE =
  "max-h-64 overflow-auto whitespace-pre-wrap break-all rounded border border-slate-300 bg-slate-50 p-2 font-mono text-xs text-slate-900";

/**
 * One plan's dry-run preview. EVERYTHING is a React text node: the block text with the
 * anchor highlighted at its code-point offsets, and the HTML snippets before and after the
 * change shown as escaped source in `<pre>` — page HTML is never rendered.
 */
export function ChangePreviewView({ preview, label }: { readonly preview: ChangePreview; readonly label?: string }) {
  const parts = previewHighlight(preview);
  const headingId = `preview-${preview.plan_id}-heading`;
  return (
    <article aria-labelledby={headingId} className="space-y-3 rounded border border-slate-300 p-3">
      <h4 id={headingId} className="font-semibold">
        <Link href={planHref(preview.plan_id)} className={`${LINK} break-words`}>
          “{label ?? preview.anchor_text}”
        </Link>
      </h4>
      <p className="text-sm">
        <span className="font-medium text-slate-700">Link address: </span>
        <code className="break-all text-xs">{preview.href}</code>
      </p>
      <figure className="space-y-1">
        <figcaption className="text-sm font-medium text-slate-800">Paragraph text with the new link</figcaption>
        {parts === null ? (
          <>
            <p className="whitespace-pre-wrap break-words text-sm text-slate-900">{preview.block_text}</p>
            <p role="note" className="rounded border border-amber-700 bg-amber-50 p-2 text-sm text-amber-950">
              <span aria-hidden="true">! </span>
              The preview offsets ({preview.anchor_start}–{preview.anchor_end}) do not select the anchor text, so nothing
              is highlighted.
            </p>
          </>
        ) : (
          <HighlightedAnchor before={parts.before} anchor={parts.match} after={parts.after} linked />
        )}
      </figure>
      <div className="grid gap-3 lg:grid-cols-2">
        <figure className="min-w-0 space-y-1">
          <figcaption className="text-sm font-medium text-slate-800">HTML before (source text, not rendered)</figcaption>
          <pre className={PRE}>{preview.html_before}</pre>
        </figure>
        <figure className="min-w-0 space-y-1">
          <figcaption className="text-sm font-medium text-slate-800">HTML after (source text, not rendered)</figcaption>
          <pre className={PRE}>{preview.html_after}</pre>
        </figure>
      </div>
    </article>
  );
}
