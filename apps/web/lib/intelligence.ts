/**
 * Presentation logic for the Phase 2 content-intelligence and job-run screens.
 *
 * Portable and pure, with the conventions of lib/status.ts and lib/inventory.ts:
 * every enum mapping is a `Record` keyed by a generated contract enum, so a new
 * contract value fails type-checking until it is labelled, and an unexpected
 * runtime value degrades to an explicit "Unknown (…)" label instead of crashing.
 *
 * Types come from `lib/api-client.ts`, never from the env-reading `lib/api.ts`.
 */
import type {
  EntityType,
  IneligibilityReason,
  Intent,
  JobKind,
  PageIntelligenceView,
  ProtectionSource,
  RelationshipType,
  TaxonomyNodeView,
  TaxonomyView,
} from "./api-client";
import { NONE } from "./format";
import { type SearchParams, firstParam } from "./inventory";
import type { StatusPresentation } from "./status";

function hasOwn<T extends object>(record: T, key: string): key is Extract<keyof T, string> {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function unknownLabel(value: string): string {
  return `Unknown (${value.trim() === "" ? "empty" : value})`;
}

function label<K extends string>(table: Readonly<Record<K, string>>, value: string): string {
  return hasOwn(table, value) ? table[value] : unknownLabel(value);
}

// ---------------------------------------------------------------------------
// Enum labels
// ---------------------------------------------------------------------------

const RELATIONSHIP_TYPE_LABEL: Readonly<Record<RelationshipType, string>> = {
  same_market: "Same market",
  parent_market: "Parent market",
  submarket: "Submarket",
  related_market: "Related market",
  same_geography: "Same geography",
  regional_parent: "Regional parent",
  supply_chain_upstream: "Supply chain: upstream",
  supply_chain_downstream: "Supply chain: downstream",
  complementary_market: "Complementary market",
  industry_adjacency: "Industry adjacency",
  semantic_similar: "Semantically similar",
};

const INELIGIBILITY_REASON_LABEL: Readonly<Record<IneligibilityReason, string>> = {
  not_published: "Not published",
  gone: "Gone (last answered 404/410)",
  protected: "Protected",
  noindex: "Robots noindex",
  not_canonical: "Not the canonical URL",
  http_not_ok: "HTTP status not OK",
  thin_content: "Thin content (below the word-count threshold)",
  page_type_excluded: "Page type is excluded from linking",
  not_analyzed: "Not analysed",
  stale_analysis: "Analysis is stale",
};

const INTENT_LABEL: Readonly<Record<Intent, string>> = {
  commercial_research: "Commercial research",
  informational: "Informational",
  navigational: "Navigational",
  transactional: "Transactional",
  corporate: "Corporate",
  unknown: "Unknown",
};

const PROTECTION_SOURCE_LABEL: Readonly<Record<ProtectionSource, string>> = {
  policy: "Policy rule",
  cms: "CMS",
  manual: "Manual (operator)",
};

const ENTITY_TYPE_LABEL: Readonly<Record<EntityType, string>> = {
  company: "Company",
  technology: "Technology",
  product: "Product",
  organization: "Organization",
};

const JOB_KIND_LABEL: Readonly<Record<JobKind, string>> = {
  analysis: "Analysis",
  recommendation: "Recommendation",
  agent: "Agent batch",
  verification: "Verification",
  analytics_import: "Analytics import",
  evaluation: "Evaluation",
  reflection: "Reflection",
  autonomy_shadow: "Autonomy shadow",
};

const NODE_KIND_LABEL: Readonly<Record<TaxonomyNodeView["kind"], string>> = {
  market: "Market",
  global: "Global",
  region: "Region",
  country: "Country",
};

const COMMERCIAL_IMPORTANCE_LABEL: readonly string[] = ["None", "Low", "Medium", "High"];

export const RELATIONSHIP_TYPES = Object.keys(RELATIONSHIP_TYPE_LABEL) as readonly RelationshipType[];
export const INELIGIBILITY_REASONS = Object.keys(INELIGIBILITY_REASON_LABEL) as readonly IneligibilityReason[];
export const JOB_KINDS = Object.keys(JOB_KIND_LABEL) as readonly JobKind[];

export const describeRelationshipType = (value: string) => label(RELATIONSHIP_TYPE_LABEL, value);
export const describeIneligibilityReason = (value: string) => label(INELIGIBILITY_REASON_LABEL, value);
export const describeIntent = (value: string) => label(INTENT_LABEL, value);
export const describeProtectionSource = (value: string) => label(PROTECTION_SOURCE_LABEL, value);
export const describeEntityType = (value: string) => label(ENTITY_TYPE_LABEL, value);
export const describeJobKind = (value: string) => label(JOB_KIND_LABEL, value);
export const describeNodeKind = (value: string) => label(NODE_KIND_LABEL, value);

/** `2` → `"Medium (2 of 3)"`. Out-of-range values are labelled unknown, never hidden. */
export function describeCommercialImportance(value: number): string {
  const name = Number.isInteger(value) ? COMMERCIAL_IMPORTANCE_LABEL[value] : undefined;
  return name === undefined ? unknownLabel(String(value)) : `${name} (${value} of 3)`;
}

export function yesNo(value: boolean): "Yes" | "No" {
  return value ? "Yes" : "No";
}

/** A score or weight to three decimals; non-finite → `NONE`. Locale-independent. */
export function formatScore(value: number | null | undefined, digits = 3): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : NONE;
}

// ---------------------------------------------------------------------------
// Freshness, eligibility and protection presentations (text label carries meaning)
// ---------------------------------------------------------------------------

export type Freshness = "fresh" | "stale" | "missing";

/** Analysis freshness: missing (none for the current version), stale (older analysis version), fresh. */
export function analysisFreshness(view: Pick<PageIntelligenceView, "analysis" | "analysis_fresh">): Freshness {
  if (view.analysis_fresh) return "fresh";
  return view.analysis === null ? "missing" : "stale";
}

/** Embedding freshness: missing (no row), stale (other version/identity), fresh. */
export function embeddingFreshness(view: Pick<PageIntelligenceView, "embedding" | "embedding_fresh">): Freshness {
  if (view.embedding_fresh) return "fresh";
  return view.embedding === null ? "missing" : "stale";
}

const ANALYSIS_FRESHNESS: Readonly<Record<Freshness, StatusPresentation>> = {
  fresh: {
    label: "Analysis fresh",
    description: "An analysis exists for the current version and the current analysis version.",
    tone: "positive",
    known: true,
  },
  stale: {
    label: "Analysis stale",
    description: "The shown analysis was produced by an older analysis version. Re-run make analyze.",
    tone: "caution",
    known: true,
  },
  missing: {
    label: "Not analysed",
    description: "The current version has no analysis yet. Run make analyze.",
    tone: "critical",
    known: true,
  },
};

const EMBEDDING_FRESHNESS: Readonly<Record<Freshness, StatusPresentation>> = {
  fresh: {
    label: "Embedding fresh",
    description: "An embedding exists for the current version and the configured embedding model.",
    tone: "positive",
    known: true,
  },
  stale: {
    label: "Embedding stale",
    description: "The stored embedding is for another version or embedding model. Re-run make analyze.",
    tone: "caution",
    known: true,
  },
  missing: {
    label: "No embedding",
    description: "The page has no stored embedding. Run make analyze with the embedding model available.",
    tone: "critical",
    known: true,
  },
};

export const describeAnalysisFreshness = (freshness: Freshness) => ANALYSIS_FRESHNESS[freshness];
export const describeEmbeddingFreshness = (freshness: Freshness) => EMBEDDING_FRESHNESS[freshness];

export function describeEligibility(eligible: boolean, role: "source" | "target"): StatusPresentation {
  return eligible
    ? {
        label: `Eligible as ${role}`,
        description: `This page may be considered as a link ${role}.`,
        tone: "positive",
        known: true,
      }
    : {
        label: `Not eligible as ${role}`,
        description: `This page is excluded as a link ${role}; see the reasons listed.`,
        tone: "caution",
        known: true,
      };
}

export function describeProtectionState(active: boolean): StatusPresentation {
  return active
    ? {
        label: "Active",
        description: "This protection blocks every mutation of, or link to, the page.",
        tone: "critical",
        known: true,
      }
    : {
        label: "Inactive",
        description: "This protection was deactivated. It is kept as history; nothing is deleted.",
        tone: "neutral",
        known: true,
      };
}

export const PROTECTED_PRESENTATION: StatusPresentation = {
  label: "Protected",
  description: "At least one active protection blocks mutation of, and links to, this page.",
  tone: "critical",
  known: true,
};

// ---------------------------------------------------------------------------
// Taxonomy
// ---------------------------------------------------------------------------

export function taxonomyNodeHref(nodeId: string): string {
  return `/intelligence/topics/${encodeURIComponent(nodeId)}`;
}

export function jobRunHref(runId: string): string {
  return `/jobs/${encodeURIComponent(runId)}`;
}

/** Node id → label, for markets, industries and geographies. */
export type TaxonomyLabels = ReadonlyMap<string, string>;

export function taxonomyLabels(taxonomy: TaxonomyView | null): TaxonomyLabels {
  const labels = new Map<string, string>();
  if (taxonomy === null) return labels;
  for (const node of [...taxonomy.markets, ...taxonomy.geographies]) {
    labels.set(node.id, node.label);
  }
  return labels;
}

/** A node's label, or its id when the taxonomy does not know it (or could not be loaded). */
export function nodeLabel(labels: TaxonomyLabels, nodeId: string): string {
  return labels.get(nodeId) ?? nodeId;
}

export interface TaxonomyTreeNode {
  readonly node: TaxonomyNodeView;
  readonly children: readonly TaxonomyTreeNode[];
}

/**
 * Nest a flat node list by `parent`, keeping the API's sibling order. A node
 * whose parent is unknown becomes a root; a parent cycle cannot recurse
 * forever, because each node is placed at most once.
 */
export function buildTaxonomyTree(nodes: readonly TaxonomyNodeView[]): TaxonomyTreeNode[] {
  const ids = new Set(nodes.map((node) => node.id));
  const childrenOf = new Map<string | null, TaxonomyNodeView[]>();
  for (const node of nodes) {
    const parent = node.parent !== null && ids.has(node.parent) && node.parent !== node.id ? node.parent : null;
    const siblings = childrenOf.get(parent) ?? [];
    siblings.push(node);
    childrenOf.set(parent, siblings);
  }
  const placed = new Set<string>();
  const build = (parent: string | null): TaxonomyTreeNode[] =>
    (childrenOf.get(parent) ?? [])
      .filter((node) => {
        if (placed.has(node.id)) return false;
        placed.add(node.id);
        return true;
      })
      .map((node) => ({ node, children: build(node.id) }));
  const roots = build(null);
  // Nodes caught in a parent cycle are unreachable from a root: surface them as roots.
  const stranded = nodes.filter((node) => !placed.has(node.id));
  for (const node of stranded) {
    if (!placed.has(node.id)) {
      placed.add(node.id);
      roots.push({ node, children: build(node.id) });
    }
  }
  return roots;
}

/** Count rows (e.g. `by_page_type`), largest first, then by key. */
export function sortCounts(counts: Readonly<Record<string, number>>): { key: string; count: number }[] {
  return Object.entries(counts)
    .map(([key, count]) => ({ key, count }))
    .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key, "en"));
}

/** `"pages_analyzed"` → `"Pages analyzed"`. For counter names, which are documented per kind. */
export function humanizeKey(key: string): string {
  const words = key.replace(/[_-]+/g, " ").trim();
  return words === "" ? key : words.charAt(0).toUpperCase() + words.slice(1);
}

// ---------------------------------------------------------------------------
// Semantic search query handling (GET form; the query is never stored)
// ---------------------------------------------------------------------------

export const SEARCH_MIN_LENGTH = 2;
export const SEARCH_MAX_LENGTH = 500;
export const SEARCH_DEFAULT_LIMIT = 10;
export const SEARCH_LIMITS: readonly number[] = [5, 10, 20, 50];
export const SIMILAR_DEFAULT_LIMIT = 10;

export type SearchQueryState =
  | { readonly kind: "empty" }
  | { readonly kind: "invalid"; readonly q: string; readonly message: string }
  | { readonly kind: "ok"; readonly q: string };

/**
 * The `q` parameter checked against the contract (2–500 characters, no NUL)
 * before any request is made, so an invalid query renders guidance, not a 422.
 */
export function parseSemanticQuery(params: SearchParams, name = "q"): SearchQueryState {
  const q = firstParam(params, name);
  if (q === undefined) return { kind: "empty" };
  if (q.includes("\u0000")) {
    return { kind: "invalid", q: q.replaceAll("\u0000", ""), message: "The query contains a character that is not allowed." };
  }
  if (q.length < SEARCH_MIN_LENGTH) {
    return { kind: "invalid", q, message: `Enter at least ${SEARCH_MIN_LENGTH} characters.` };
  }
  if (q.length > SEARCH_MAX_LENGTH) {
    return { kind: "invalid", q: q.slice(0, SEARCH_MAX_LENGTH), message: `Use at most ${SEARCH_MAX_LENGTH} characters.` };
  }
  return { kind: "ok", q };
}

/** The search `limit`, accepted only from the offered choices; anything else → the default. */
export function parseSearchLimit(params: SearchParams, name = "limit"): number {
  const raw = firstParam(params, name);
  const value = raw !== undefined && /^\d{1,3}$/.test(raw) ? Number.parseInt(raw, 10) : undefined;
  return value !== undefined && SEARCH_LIMITS.includes(value) ? value : SEARCH_DEFAULT_LIMIT;
}
