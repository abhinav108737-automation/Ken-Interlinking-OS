import type { CorpusBanner as CorpusBannerData } from "@/lib/api-client";
import { describeCorpusBanner } from "@/lib/learning";

/**
 * The persistent corpus banner (ADR-0016 §16.1). It is on EVERY learning screen
 * and is not dismissible, because the corpus is small and synthetic: every
 * number these screens show is a demonstration, not evidence, and no proposal
 * on this data may be promoted.
 *
 * `note` is a fixed sentence the API composes and is never model output; it is
 * rendered as text, and the counts are shown beside it so the claim is
 * checkable rather than merely asserted.
 */
export function CorpusBanner({ banner }: { readonly banner: CorpusBannerData }) {
  const presentation = describeCorpusBanner(banner);
  return (
    <div role="alert" className="rounded border-2 border-amber-700 bg-amber-50 p-3 text-amber-950">
      <p className="font-semibold">
        <span aria-hidden="true">! </span>
        {presentation.title}
      </p>
      <p className="max-w-prose text-sm">{presentation.note}</p>
      <p className="max-w-prose text-sm">{presentation.enforcement}</p>
      <dl className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-sm">
        {presentation.counts.map(([term, value]) => (
          <div key={term} className="flex gap-1">
            <dt className="font-medium">{term}</dt>
            <dd className="tabular-nums">{value}</dd>
          </div>
        ))}
        <div className="flex gap-1">
          <dt className="font-medium">Minimum samples per decision class</dt>
          <dd className="tabular-nums">{presentation.minSampleCount}</dd>
        </div>
        <div className="flex gap-1">
          <dt className="font-medium">Demonstration mode</dt>
          <dd>{presentation.demonstrationMode ? "on" : "off"}</dd>
        </div>
      </dl>
    </div>
  );
}
