import Link from "next/link";
import type { ReactNode } from "react";

import type {
  ApiResult,
  LinkEligibility,
  PageAnalysis,
  PageEmbeddingInfo,
  PageIntelligenceView,
  PageProtectionView,
  RelationshipList,
  SimilarPageList,
} from "@/lib/api-client";
import { formatCount, formatDateTime, shortHash } from "@/lib/format";
import {
  type TaxonomyLabels,
  analysisFreshness,
  describeAnalysisFreshness,
  describeCommercialImportance,
  describeEligibility,
  describeEmbeddingFreshness,
  describeEntityType,
  describeIneligibilityReason,
  describeIntent,
  describeNodeKind,
  describeProtectionSource,
  describeProtectionState,
  describeRelationshipType,
  embeddingFreshness,
  formatScore,
  nodeLabel,
  PROTECTED_PRESENTATION,
  sortCounts,
  taxonomyNodeHref,
} from "@/lib/intelligence";

import { ApiErrorNotice } from "../ApiErrorNotice";
import { StatusBadge } from "../StatusBadge";
import { Pagination } from "../inventory/Pagination";
import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";
import { CountTable } from "./CountTable";
import { RelationshipTable } from "./RelationshipTable";
import { SimilarPageTable } from "./SimilarPageTable";

const FACTS = "grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm";
const LINK = "text-blue-800 underline underline-offset-2";

function Fact({ term, children }: { readonly term: string; readonly children: ReactNode }) {
  return (
    <>
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd className="min-w-0 text-slate-900">{children}</dd>
    </>
  );
}

function Muted({ children }: { readonly children: ReactNode }) {
  return <span className="text-slate-600">{children}</span>;
}

function Sub({ id, title, children }: { readonly id: string; readonly title: string; readonly children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="space-y-2">
      <h3 id={id} className="text-lg font-semibold">
        {title}
      </h3>
      {children}
    </section>
  );
}

function NodeLink({ id, labels }: { readonly id: string | null; readonly labels: TaxonomyLabels }) {
  if (id === null) return <Muted>None identified</Muted>;
  return (
    <Link href={taxonomyNodeHref(id)} className={LINK}>
      {nodeLabel(labels, id)}
    </Link>
  );
}

function Classification({ analysis, labels }: { readonly analysis: PageAnalysis; readonly labels: TaxonomyLabels }) {
  return (
    <dl className={FACTS}>
      <Fact term="Page type">{analysis.page_type}</Fact>
      <Fact term="Intent">{describeIntent(analysis.intent)}</Fact>
      <Fact term="Commercial importance">{describeCommercialImportance(analysis.commercial_importance)}</Fact>
      <Fact term="Industry">
        <NodeLink id={analysis.industry_id} labels={labels} />
      </Fact>
      <Fact term="Market">
        <NodeLink id={analysis.market_id} labels={labels} />
      </Fact>
      <Fact term="Analysed version">v{analysis.page_version}</Fact>
      <Fact term="Analysis version">
        <code className="text-xs">{analysis.analysis_version}</code>
      </Fact>
      <Fact term="Extractor">{analysis.extractor}</Fact>
    </dl>
  );
}

/** Topics and geographies carry their own labels from the analysis. */
function AnalysisDetails({ analysis }: { readonly analysis: PageAnalysis }) {
  return (
    <>
      <Sub id="intel-topics-heading" title="Topics">
        <TableFrame caption="Topics" count={analysis.topics.length}>
          <HeaderRow columns={["Market", "Depth", "Score"]} />
          <tbody>
            {analysis.topics.length === 0 ? (
              <EmptyRow colSpan={3}>No markets were matched.</EmptyRow>
            ) : (
              analysis.topics.map((topic) => (
                <tr key={topic.node_id} className={ROW}>
                  <th scope="row" className={`${CELL} font-medium`}>
                    <Link href={taxonomyNodeHref(topic.node_id)} className={LINK}>
                      {topic.label}
                    </Link>
                  </th>
                  <td className={CELL}>{topic.depth === 0 ? "Industry" : `Level ${topic.depth}`}</td>
                  <td className={NUM_CELL}>{formatScore(topic.score)}</td>
                </tr>
              ))
            )}
          </tbody>
        </TableFrame>
      </Sub>

      <Sub id="intel-geographies-heading" title="Geographies">
        <TableFrame caption="Geographies" count={analysis.geographies.length}>
          <HeaderRow columns={["Geography", "Kind", "Score"]} />
          <tbody>
            {analysis.geographies.length === 0 ? (
              <EmptyRow colSpan={3}>No geographies were matched.</EmptyRow>
            ) : (
              analysis.geographies.map((geo) => (
                <tr key={geo.geo_id} className={ROW}>
                  <th scope="row" className={`${CELL} font-medium`}>
                    <Link href={taxonomyNodeHref(geo.geo_id)} className={LINK}>
                      {geo.label}
                    </Link>
                  </th>
                  <td className={CELL}>{describeNodeKind(geo.kind)}</td>
                  <td className={NUM_CELL}>{formatScore(geo.score)}</td>
                </tr>
              ))
            )}
          </tbody>
        </TableFrame>
      </Sub>

      <Sub id="intel-entities-heading" title="Entities">
        <TableFrame caption="Entities" count={analysis.entities.length}>
          <HeaderRow columns={["Entity", "Type", "Mentions"]} />
          <tbody>
            {analysis.entities.length === 0 ? (
              <EmptyRow colSpan={3}>No known entities were mentioned.</EmptyRow>
            ) : (
              analysis.entities.map((entity) => (
                <tr key={entity.entity_id} className={ROW}>
                  <th scope="row" className={`${CELL} font-medium`}>
                    {entity.label}
                  </th>
                  <td className={CELL}>{describeEntityType(entity.type)}</td>
                  <td className={NUM_CELL}>{formatCount(entity.mentions)}</td>
                </tr>
              ))
            )}
          </tbody>
        </TableFrame>
      </Sub>

      <Sub id="intel-keywords-heading" title="Keywords">
        {analysis.keywords.length === 0 ? (
          <p className="text-slate-700">No keywords were extracted.</p>
        ) : (
          <ul aria-label="Keywords" className="flex flex-wrap gap-2 text-sm">
            {analysis.keywords.map((keyword, index) => (
              <li key={`${index}:${keyword}`} className="rounded border border-slate-300 bg-slate-50 px-2 py-0.5">
                {keyword}
              </li>
            ))}
          </ul>
        )}
      </Sub>

      <Sub id="intel-summary-heading" title="Extractive summary">
        {analysis.summary.trim() === "" ? (
          <p className="text-slate-700">No summary was extracted.</p>
        ) : (
          <p className="max-w-prose whitespace-pre-wrap break-words text-sm text-slate-900">{analysis.summary}</p>
        )}
      </Sub>

      <Sub id="intel-salient-heading" title="Salient blocks">
        {analysis.salient_blocks.length === 0 ? (
          <p className="text-slate-700">No salient blocks were identified.</p>
        ) : (
          <ol className="space-y-2 text-sm">
            {analysis.salient_blocks.map((block) => (
              <li key={block.block_id} className="rounded border border-slate-300 p-2">
                <p className="text-xs text-slate-700">
                  Block <span className="font-mono">{block.block_id}</span> · score {formatScore(block.score)}
                </p>
                <p className="whitespace-pre-wrap break-words text-slate-900">{block.text}</p>
              </li>
            ))}
          </ol>
        )}
      </Sub>
    </>
  );
}

function EmbeddingFacts({
  embedding,
  currentVersion,
}: {
  readonly embedding: PageEmbeddingInfo | null;
  readonly currentVersion: number;
}) {
  if (embedding === null) {
    return <p className="text-slate-700">No embedding is stored for this page.</p>;
  }
  return (
    <dl className={FACTS}>
      <Fact term="Model">
        <code className="text-xs">{embedding.model}</code>
      </Fact>
      <Fact term="Provider">{embedding.provider}</Fact>
      <Fact term="Dimensions">{formatCount(embedding.dimensions)}</Fact>
      <Fact term="Recipe version">{embedding.recipe_version}</Fact>
      <Fact term="Embedded version">
        v{embedding.page_version}
        {embedding.page_version !== currentVersion && <Muted> (current is v{currentVersion})</Muted>}
      </Fact>
      <Fact term="Input hash">
        <code className="text-xs" title={embedding.input_hash}>
          {shortHash(embedding.input_hash)}
        </code>
      </Fact>
      <Fact term="Created">
        <time dateTime={embedding.created_at}>{formatDateTime(embedding.created_at)}</time>
      </Fact>
    </dl>
  );
}

const PROTECTION_COLUMNS = ["State", "Source", "Rule", "Reason", "Created by", "Created"];

function ProtectionTable({ protections }: { readonly protections: readonly PageProtectionView[] }) {
  // Active protections first, then history; each group keeps the API order.
  const ordered = [...protections.filter((p) => p.active), ...protections.filter((p) => !p.active)];
  return (
    <TableFrame caption="Protections" count={protections.length}>
      <HeaderRow columns={PROTECTION_COLUMNS} />
      <tbody>
        {ordered.length === 0 ? (
          <EmptyRow colSpan={PROTECTION_COLUMNS.length}>No protections are recorded for this page.</EmptyRow>
        ) : (
          ordered.map((protection) => (
            <tr key={protection.id} className={ROW}>
              <td className={CELL}>
                <StatusBadge presentation={describeProtectionState(protection.active)} />
              </td>
              <td className={CELL}>{describeProtectionSource(protection.source)}</td>
              <td className={CELL}>
                {protection.rule_id === null ? <Muted>—</Muted> : <code className="text-xs">{protection.rule_id}</code>}
              </td>
              <td className={`${CELL} break-words`}>{protection.reason}</td>
              <td className={CELL}>{protection.created_by}</td>
              <td className={`${CELL} whitespace-nowrap`}>
                <time dateTime={protection.created_at}>{formatDateTime(protection.created_at)}</time>
              </td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}

function EligibilityRole({
  role,
  eligible,
  reasons,
}: {
  readonly role: "source" | "target";
  readonly eligible: boolean;
  readonly reasons: readonly string[];
}) {
  return (
    <div className="space-y-1">
      <dt className="font-medium text-slate-700">As a link {role}</dt>
      <dd>
        <StatusBadge presentation={describeEligibility(eligible, role)} />
      </dd>
      <dd>
        {reasons.length === 0 ? (
          eligible ? null : <Muted>No reason was given.</Muted>
        ) : (
          <ul aria-label={`Reasons not eligible as ${role}`} className="list-disc pl-5 text-sm">
            {reasons.map((reason) => (
              <li key={reason}>{describeIneligibilityReason(reason)}</li>
            ))}
          </ul>
        )}
      </dd>
    </div>
  );
}

function Eligibility({ eligibility }: { readonly eligibility: LinkEligibility }) {
  return (
    <dl className="grid gap-4 text-sm sm:grid-cols-2">
      <EligibilityRole role="source" eligible={eligibility.as_source} reasons={eligibility.source_reasons} />
      <EligibilityRole role="target" eligible={eligibility.as_target} reasons={eligibility.target_reasons} />
    </dl>
  );
}

export interface PageIntelligencePanelProps {
  readonly intelligence: ApiResult<PageIntelligenceView>;
  readonly relationships: ApiResult<RelationshipList>;
  readonly similar: ApiResult<SimilarPageList>;
  /** Taxonomy node labels; empty when the taxonomy could not be loaded (ids are shown instead). */
  readonly labels: TaxonomyLabels;
  /** Where the related-pages pagination links point, and the state they preserve. */
  readonly pagination: {
    readonly pathname: string;
    readonly params: Readonly<Record<string, string | number | null | undefined>>;
    readonly offsetParam: string;
  };
}

/**
 * The "Intelligence" section of a page's detail screen: read-only inspection of
 * the deterministic analysis, embedding, protections, eligibility and derived
 * relationships of the page's CURRENT version. Model- and page-derived text is
 * rendered as React text nodes only — never as HTML.
 */
export function PageIntelligencePanel({
  intelligence,
  relationships,
  similar,
  labels,
  pagination,
}: PageIntelligencePanelProps) {
  if (!intelligence.ok) {
    return (
      <section aria-labelledby="intelligence-heading" className="space-y-3">
        <h2 id="intelligence-heading" className="text-xl font-semibold">
          Intelligence
        </h2>
        <ApiErrorNotice error={intelligence.error} what="Page intelligence" />
      </section>
    );
  }

  const view = intelligence.data;
  const analysisState = analysisFreshness(view);
  const embeddingState = embeddingFreshness(view);
  const activeProtections = view.protections.filter((p) => p.active).length;
  const relationshipRows = sortCounts(view.relationship_counts).map(({ key, count }) => ({
    key,
    label: describeRelationshipType(key),
    count,
  }));

  return (
    <section aria-labelledby="intelligence-heading" className="space-y-6">
      <header className="space-y-2">
        <h2 id="intelligence-heading" className="text-xl font-semibold">
          Intelligence
        </h2>
        <p className="text-sm text-slate-700">
          Deterministic analysis of the current version (v{view.current_version}). Read-only; analysis runs
          start from the command line (<code>make analyze</code>).
        </p>
        <p className="flex flex-wrap items-center gap-2">
          <StatusBadge presentation={describeAnalysisFreshness(analysisState)} />
          <StatusBadge presentation={describeEmbeddingFreshness(embeddingState)} />
          {activeProtections > 0 && <StatusBadge presentation={PROTECTED_PRESENTATION} />}
        </p>
      </header>

      <Sub id="intel-classification-heading" title="Classification">
        {view.analysis === null ? (
          <p role="status" className="rounded border border-slate-300 bg-slate-50 p-3 text-slate-800">
            The current version (v{view.current_version}) has not been analysed yet. Run{" "}
            <code>make analyze</code> to classify it.
          </p>
        ) : (
          <>
            {analysisState === "stale" && (
              <p role="status" className="rounded border border-amber-700 bg-amber-50 p-3 text-amber-950">
                This analysis is stale: it was produced by analysis version{" "}
                <code>{view.analysis.analysis_version}</code>, not the current one. Re-run{" "}
                <code>make analyze</code> to refresh it.
              </p>
            )}
            <Classification analysis={view.analysis} labels={labels} />
          </>
        )}
      </Sub>

      {view.analysis !== null && <AnalysisDetails analysis={view.analysis} />}

      <Sub id="intel-embedding-heading" title="Embedding">
        <EmbeddingFacts embedding={view.embedding} currentVersion={view.current_version} />
      </Sub>

      <Sub id="intel-protections-heading" title="Protections">
        <p className="text-sm text-slate-700">
          {activeProtections === 0
            ? "No active protection. History is kept; protections are deactivated, never deleted."
            : `${formatCount(activeProtections)} active ${activeProtections === 1 ? "protection blocks" : "protections block"} every change to, and link to, this page.`}
        </p>
        <ProtectionTable protections={view.protections} />
      </Sub>

      <Sub id="intel-eligibility-heading" title="Link eligibility">
        <p className="text-sm text-slate-700">Computed by the API from current state when this page was loaded.</p>
        <Eligibility eligibility={view.eligibility} />
      </Sub>

      <Sub id="intel-relationship-counts-heading" title="Relationship counts">
        <CountTable
          caption="Relationships by type"
          keyHeader="Relationship type"
          valueHeader="Edges"
          rows={relationshipRows}
          emptyMessage="No relationships have been computed for this page."
        />
      </Sub>

      <Sub id="intel-related-heading" title="Related pages">
        {relationships.ok ? (
          <>
            <RelationshipTable
              relationships={relationships.data.items}
              total={relationships.data.page.total}
              emptyMessage="No current relationships from this page."
            />
            <Pagination
              page={relationships.data.page}
              itemCount={relationships.data.items.length}
              pathname={pagination.pathname}
              params={pagination.params}
              offsetParam={pagination.offsetParam}
              label="Related pages pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={relationships.error} what="Related pages" />
        )}
      </Sub>

      <Sub id="intel-similar-heading" title="Similar pages">
        {similar.ok ? (
          <SimilarPageTable
            pages={similar.data.items}
            caption="Similar pages"
            labels={labels}
            scoreHeader="Similarity"
            emptyMessage={
              embeddingState === "fresh"
                ? "No similar pages were found."
                : "No similar pages: this page has no fresh embedding."
            }
          />
        ) : (
          <ApiErrorNotice error={similar.error} what="Similar pages" />
        )}
      </Sub>
    </section>
  );
}
