import type {
  IntelligenceCoverage,
  JobRunDetail,
  JobRunSummary,
  PageAnalysis,
  PageEmbeddingInfo,
  PageIntelligenceView,
  PageProtectionView,
  PageRelationshipView,
  SimilarPage,
  TaxonomyNodeView,
  TaxonomyView,
} from "@/lib/api-client";

import { OTHER_PAGE_ID, PAGE_ID } from "./inventory";

export const JOB_RUN_ID = "44444444-4444-4444-8444-444444444444";
export const THIRD_PAGE_ID = "55555555-5555-4555-8555-555555555555";

export function node(overrides: Partial<TaxonomyNodeView> & Pick<TaxonomyNodeView, "id" | "label">): TaxonomyNodeView {
  return { kind: "market", parent: null, depth: 0, page_count: 0, ...overrides };
}

export function taxonomy(overrides: Partial<TaxonomyView> = {}): TaxonomyView {
  return {
    version: 1,
    status: "active",
    source: "fixture",
    analysis_version: "tax1-alg1",
    markets: [
      node({ id: "energy", label: "Energy", page_count: 4 }),
      node({ id: "energy.batteries", label: "Batteries", parent: "energy", depth: 1, page_count: 3 }),
      node({ id: "energy.batteries.ev", label: "EV batteries", parent: "energy.batteries", depth: 2, page_count: 2 }),
      node({ id: "healthcare", label: "Healthcare", page_count: 1 }),
    ],
    geographies: [
      node({ id: "global", label: "Global", kind: "global", page_count: 5 }),
      node({ id: "asia", label: "Asia", kind: "region", parent: "global", depth: 1, page_count: 2 }),
      node({ id: "in", label: "India", kind: "country", parent: "asia", depth: 2, page_count: 1 }),
    ],
    page_types: [
      {
        id: "report",
        label: "Market report",
        intent: "commercial_research",
        commercial_importance: 3,
        linkable_as_source: true,
        linkable_as_target: true,
        page_count: 4,
      },
      {
        id: "legal",
        label: "Legal page",
        intent: "corporate",
        commercial_importance: 0,
        linkable_as_source: false,
        linkable_as_target: false,
        page_count: 1,
      },
    ],
    ...overrides,
  };
}

export function analysis(overrides: Partial<PageAnalysis> = {}): PageAnalysis {
  return {
    page_id: PAGE_ID,
    page_version: 2,
    analysis_version: "tax1-alg1",
    extractor: "deterministic",
    page_type: "report",
    intent: "commercial_research",
    commercial_importance: 3,
    industry_id: "energy",
    market_id: "energy.batteries.ev",
    topics: [
      { node_id: "energy.batteries.ev", label: "EV batteries", score: 4.5, depth: 2 },
      { node_id: "energy", label: "Energy", score: 1.25, depth: 0 },
    ],
    geographies: [{ geo_id: "in", label: "India", kind: "country", score: 2 }],
    entities: [{ entity_id: "acme", label: "Acme Corp", type: "company", mentions: 3 }],
    keywords: ["battery", "lithium", "cathode"],
    summary: "The EV battery market in India <b>grows</b> fast.",
    salient_blocks: [{ block_id: "b7", score: 0.9, text: "Lithium <script>window.__pwned = true</script> demand" }],
    ...overrides,
  };
}

export function embedding(overrides: Partial<PageEmbeddingInfo> = {}): PageEmbeddingInfo {
  return {
    page_id: PAGE_ID,
    page_version: 2,
    model: "nomic-embed-text",
    provider: "ollama",
    dimensions: 768,
    recipe_version: 1,
    recipe_fingerprint: "r1-0123456789abcdef",
    input_hash: "ab".repeat(32),
    created_at: "2026-10-14T08:00:00Z",
    ...overrides,
  };
}

export function protection(overrides: Partial<PageProtectionView> = {}): PageProtectionView {
  return {
    id: 1,
    page_id: PAGE_ID,
    source: "policy",
    rule_id: "legal-pages",
    reason: "Legal pages are never modified.",
    active: true,
    created_by: "analysis",
    created_at: "2026-10-14T08:00:00Z",
    ...overrides,
  };
}

export function pageIntelligence(overrides: Partial<PageIntelligenceView> = {}): PageIntelligenceView {
  return {
    page_id: PAGE_ID,
    canonical_url: "https://www.example.com/reports/widgets",
    title: "Widgets market report",
    current_version: 2,
    analysis: analysis(),
    analysis_fresh: true,
    embedding: embedding(),
    embedding_fresh: true,
    protections: [],
    eligibility: { as_source: true, as_target: true, source_reasons: [], target_reasons: [] },
    relationship_counts: { same_market: 2, semantic_similar: 5 },
    ...overrides,
  };
}

export function relationship(overrides: Partial<PageRelationshipView> = {}): PageRelationshipView {
  return {
    source_page_id: PAGE_ID,
    source_page_version: 2,
    target_page_id: OTHER_PAGE_ID,
    target_page_version: 1,
    target_canonical_url: "https://www.example.com/reports/gadgets",
    target_title: "Gadgets report",
    relationship_type: "same_market",
    weight: 0.85,
    evidence: { shared_nodes: ["energy.batteries.ev"], cosine: 0.91 },
    analysis_version: "tax1-alg1",
    computed_at: "2026-10-14T08:01:00Z",
    ...overrides,
  };
}

export function similarPage(overrides: Partial<SimilarPage> = {}): SimilarPage {
  return {
    page_id: OTHER_PAGE_ID,
    canonical_url: "https://www.example.com/reports/gadgets",
    title: "Gadgets report",
    score: 0.8123,
    page_type: "report",
    market_id: "energy.batteries",
    ...overrides,
  };
}

export function coverage(overrides: Partial<IntelligenceCoverage> = {}): IntelligenceCoverage {
  return {
    total_pages: 1200,
    analysis_version: "tax1-alg1",
    analyzed_fresh: 1000,
    analyzed_stale: 150,
    never_analyzed: 50,
    embedding_model: "nomic-embed-text",
    embedded_fresh: 990,
    embedded_stale_or_missing: 210,
    protected: 12,
    eligible_sources: 870,
    eligible_targets: 910,
    relationships: 15432,
    by_page_type: { report: 900, legal: 30, unknown_type: 70 },
    by_industry: { energy: 400, healthcare: 600, retired_industry: 2 },
    ...overrides,
  };
}

export function jobRunSummary(overrides: Partial<JobRunSummary> = {}): JobRunSummary {
  return {
    id: JOB_RUN_ID,
    kind: "analysis",
    status: "succeeded",
    triggered_by: "cli",
    started_at: "2026-10-14T08:00:00Z",
    completed_at: "2026-10-14T08:00:42Z",
    counters: { pages_analyzed: 120, pages_skipped: 3, embeddings_written: 118 },
    ...overrides,
  };
}

export function jobRunDetail(overrides: Partial<JobRunDetail> = {}): JobRunDetail {
  return {
    ...jobRunSummary(),
    config_snapshot: {
      analysis_version: "tax1-alg1",
      batch_size: 50,
      embedding_model: "nomic-embed-text",
      db_password: "should-never-show",
      provider: { name: "ollama", api_key: "nope" },
    },
    failure_reason: null,
    ...overrides,
  };
}
