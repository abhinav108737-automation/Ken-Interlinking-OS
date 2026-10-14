import { describe, expect, it } from "vitest";

import type { TaxonomyNodeView } from "@/lib/api-client";
import {
  INELIGIBILITY_REASONS,
  JOB_KINDS,
  RELATIONSHIP_TYPES,
  SEARCH_DEFAULT_LIMIT,
  analysisFreshness,
  buildTaxonomyTree,
  describeAnalysisFreshness,
  describeCommercialImportance,
  describeEligibility,
  describeEmbeddingFreshness,
  describeIneligibilityReason,
  describeIntent,
  describeJobKind,
  describeNodeKind,
  describeProtectionSource,
  describeProtectionState,
  describeRelationshipType,
  embeddingFreshness,
  formatScore,
  humanizeKey,
  jobRunHref,
  nodeLabel,
  parseSearchLimit,
  parseSemanticQuery,
  sortCounts,
  taxonomyLabels,
  taxonomyNodeHref,
} from "@/lib/intelligence";

import { analysis, embedding, node, taxonomy } from "./fixtures/intelligence";

describe("enum labels", () => {
  it("labels every ineligibility reason in plain words", () => {
    expect(INELIGIBILITY_REASONS).toHaveLength(10);
    for (const reason of INELIGIBILITY_REASONS) {
      const text = describeIneligibilityReason(reason);
      expect(text).not.toMatch(/^Unknown/);
      expect(text).not.toContain("_");
    }
    expect(describeIneligibilityReason("thin_content")).toBe("Thin content (below the word-count threshold)");
    expect(describeIneligibilityReason("noindex")).toBe("Robots noindex");
    expect(describeIneligibilityReason("stale_analysis")).toBe("Analysis is stale");
  });

  it("labels every relationship type", () => {
    expect(RELATIONSHIP_TYPES).toHaveLength(11);
    for (const type of RELATIONSHIP_TYPES) {
      expect(describeRelationshipType(type)).not.toMatch(/^Unknown|_/);
    }
    expect(describeRelationshipType("supply_chain_upstream")).toBe("Supply chain: upstream");
    expect(describeRelationshipType("semantic_similar")).toBe("Semantically similar");
  });

  it("labels job kinds, intents, protection sources and node kinds", () => {
    expect(JOB_KINDS.map(describeJobKind)).toContain("Analytics import");
    expect(describeIntent("commercial_research")).toBe("Commercial research");
    expect(describeProtectionSource("manual")).toBe("Manual (operator)");
    expect(describeNodeKind("country")).toBe("Country");
  });

  it.each([
    [describeIneligibilityReason, "brand_new"],
    [describeRelationshipType, "brand_new"],
    [describeJobKind, "brand_new"],
    [describeIntent, ""],
  ])("degrades an unexpected value to Unknown", (describe, value) => {
    expect(describe(value)).toBe(`Unknown (${value === "" ? "empty" : value})`);
  });

  it("describes commercial importance on its 0–3 scale", () => {
    expect(describeCommercialImportance(0)).toBe("None (0 of 3)");
    expect(describeCommercialImportance(3)).toBe("High (3 of 3)");
    expect(describeCommercialImportance(4)).toBe("Unknown (4)");
    expect(describeCommercialImportance(1.5)).toBe("Unknown (1.5)");
  });

  it("formats scores locale-independently", () => {
    expect(formatScore(0.81234)).toBe("0.812");
    expect(formatScore(4.5)).toBe("4.500");
    expect(formatScore(Number.NaN)).toBe("—");
    expect(formatScore(null)).toBe("—");
  });
});

describe("freshness", () => {
  it("distinguishes fresh, stale and missing analysis", () => {
    expect(analysisFreshness({ analysis: analysis(), analysis_fresh: true })).toBe("fresh");
    expect(analysisFreshness({ analysis: analysis(), analysis_fresh: false })).toBe("stale");
    expect(analysisFreshness({ analysis: null, analysis_fresh: false })).toBe("missing");
  });

  it("distinguishes fresh, stale and missing embeddings", () => {
    expect(embeddingFreshness({ embedding: embedding(), embedding_fresh: true })).toBe("fresh");
    expect(embeddingFreshness({ embedding: embedding(), embedding_fresh: false })).toBe("stale");
    expect(embeddingFreshness({ embedding: null, embedding_fresh: false })).toBe("missing");
  });

  it("gives every state a self-explanatory label and a tone", () => {
    expect(describeAnalysisFreshness("fresh")).toMatchObject({ label: "Analysis fresh", tone: "positive" });
    expect(describeAnalysisFreshness("stale")).toMatchObject({ label: "Analysis stale", tone: "caution" });
    expect(describeAnalysisFreshness("missing")).toMatchObject({ label: "Not analysed", tone: "critical" });
    expect(describeEmbeddingFreshness("stale").label).toBe("Embedding stale");
    expect(describeEmbeddingFreshness("missing").label).toBe("No embedding");
  });

  it("labels eligibility and protection states in text", () => {
    expect(describeEligibility(true, "source").label).toBe("Eligible as source");
    expect(describeEligibility(false, "target")).toMatchObject({ label: "Not eligible as target", tone: "caution" });
    expect(describeProtectionState(true)).toMatchObject({ label: "Active", tone: "critical" });
    expect(describeProtectionState(false)).toMatchObject({ label: "Inactive", tone: "neutral" });
  });
});

describe("taxonomy helpers", () => {
  it("builds hrefs with encoded ids", () => {
    expect(taxonomyNodeHref("energy.batteries")).toBe("/intelligence/topics/energy.batteries");
    expect(taxonomyNodeHref("a/b")).toBe("/intelligence/topics/a%2Fb");
    expect(jobRunHref("x y")).toBe("/jobs/x%20y");
  });

  it("maps node ids to labels and falls back to the id", () => {
    const labels = taxonomyLabels(taxonomy());
    expect(nodeLabel(labels, "energy.batteries.ev")).toBe("EV batteries");
    expect(nodeLabel(labels, "in")).toBe("India");
    expect(nodeLabel(labels, "unknown.node")).toBe("unknown.node");
    expect(nodeLabel(taxonomyLabels(null), "energy")).toBe("energy");
  });

  it("nests nodes by parent, keeping sibling order", () => {
    const tree = buildTaxonomyTree(taxonomy().markets);
    expect(tree.map((t) => t.node.id)).toEqual(["energy", "healthcare"]);
    expect(tree[0]!.children.map((t) => t.node.id)).toEqual(["energy.batteries"]);
    expect(tree[0]!.children[0]!.children.map((t) => t.node.id)).toEqual(["energy.batteries.ev"]);
  });

  it("makes orphans roots and survives parent cycles and self-parents", () => {
    const nodes: TaxonomyNodeView[] = [
      node({ id: "orphan", label: "Orphan", parent: "missing" }),
      node({ id: "self", label: "Self", parent: "self" }),
      node({ id: "a", label: "A", parent: "b" }),
      node({ id: "b", label: "B", parent: "a" }),
    ];
    const tree = buildTaxonomyTree(nodes);
    const ids: string[] = [];
    const walk = (items: typeof tree) =>
      items.forEach((item) => {
        ids.push(item.node.id);
        walk([...item.children]);
      });
    walk(tree);
    expect(ids.sort()).toEqual(["a", "b", "orphan", "self"]);
  });

  it("sorts counts largest first, then by key", () => {
    expect(sortCounts({ b: 1, a: 1, c: 5 })).toEqual([
      { key: "c", count: 5 },
      { key: "a", count: 1 },
      { key: "b", count: 1 },
    ]);
  });

  it("humanizes counter keys", () => {
    expect(humanizeKey("pages_analyzed")).toBe("Pages analyzed");
    expect(humanizeKey("embeddings-written")).toBe("Embeddings written");
    expect(humanizeKey("___")).toBe("___");
  });
});

describe("semantic search parameters", () => {
  it("treats absent or blank q as empty", () => {
    expect(parseSemanticQuery({})).toEqual({ kind: "empty" });
    expect(parseSemanticQuery({ q: "   " })).toEqual({ kind: "empty" });
  });

  it("rejects a too-short, too-long or NUL-containing query before any request", () => {
    expect(parseSemanticQuery({ q: "a" })).toMatchObject({ kind: "invalid", q: "a", message: "Enter at least 2 characters." });
    const long = parseSemanticQuery({ q: "x".repeat(501) });
    expect(long).toMatchObject({ kind: "invalid", message: "Use at most 500 characters." });
    if (long.kind !== "invalid") throw new Error("unreachable");
    expect(long.q).toHaveLength(500);
    expect(parseSemanticQuery({ q: "ab\u0000c" })).toMatchObject({ kind: "invalid", q: "abc" });
  });

  it("accepts a trimmed 2–500 character query, first value only", () => {
    expect(parseSemanticQuery({ q: "  ev batteries " })).toEqual({ kind: "ok", q: "ev batteries" });
    expect(parseSemanticQuery({ q: ["lithium", "other"] })).toEqual({ kind: "ok", q: "lithium" });
    expect(parseSemanticQuery({ q: "x".repeat(500) }).kind).toBe("ok");
  });

  it("accepts only the offered limits", () => {
    expect(parseSearchLimit({})).toBe(SEARCH_DEFAULT_LIMIT);
    expect(parseSearchLimit({ limit: "20" })).toBe(20);
    expect(parseSearchLimit({ limit: "51" })).toBe(SEARCH_DEFAULT_LIMIT);
    expect(parseSearchLimit({ limit: "-5" })).toBe(SEARCH_DEFAULT_LIMIT);
    expect(parseSearchLimit({ limit: "abc" })).toBe(SEARCH_DEFAULT_LIMIT);
  });
});
