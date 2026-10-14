import type { IntelligenceCoverage } from "@/lib/api-client";
import { formatCount } from "@/lib/format";

interface Card {
  readonly term: string;
  readonly value: number;
  /** Whether the value counts pages (shown "of N pages"). */
  readonly ofPages: boolean;
}

function cards(coverage: IntelligenceCoverage): readonly Card[] {
  return [
    { term: "Analysed (fresh)", value: coverage.analyzed_fresh, ofPages: true },
    { term: "Analysed (stale)", value: coverage.analyzed_stale, ofPages: true },
    { term: "Never analysed", value: coverage.never_analyzed, ofPages: true },
    { term: "Embedded (fresh)", value: coverage.embedded_fresh, ofPages: true },
    { term: "Embedding stale or missing", value: coverage.embedded_stale_or_missing, ofPages: true },
    { term: "Protected", value: coverage.protected, ofPages: true },
    { term: "Eligible link sources", value: coverage.eligible_sources, ofPages: true },
    { term: "Eligible link targets", value: coverage.eligible_targets, ofPages: true },
    { term: "Relationships", value: coverage.relationships, ofPages: false },
  ];
}

/** Coverage as a description list of cards. Every value is text, never colour alone. */
export function CoverageSummary({ coverage }: { readonly coverage: IntelligenceCoverage }) {
  const total = formatCount(coverage.total_pages);
  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
        <dt className="font-medium text-slate-700">Pages in inventory</dt>
        <dd className="tabular-nums">{total}</dd>
        <dt className="font-medium text-slate-700">Analysis version</dt>
        <dd>
          <code className="text-xs">{coverage.analysis_version}</code>
        </dd>
        <dt className="font-medium text-slate-700">Embedding model</dt>
        <dd>
          <code className="text-xs">{coverage.embedding_model}</code>
        </dd>
      </dl>
      <dl aria-label="Coverage counts" className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {cards(coverage).map((card) => (
          <div key={card.term} className="rounded border border-slate-300 bg-slate-50 p-3">
            <dt className="text-sm font-medium text-slate-700">{card.term}</dt>
            <dd className="text-2xl font-semibold tabular-nums text-slate-900">
              {formatCount(card.value)}
              {card.ofPages && <span className="ml-1 text-sm font-normal text-slate-700">of {total}</span>}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
