import { describe, expect, it } from "vitest";

import type { components } from "@/lib/types.gen";
import {
  ALL_STATUSES,
  CANDIDATE_REJECTIONS,
  LINK_PLAN_STATUSES,
  OPPORTUNITY_KINDS,
  REASON_CODES,
  checkScore,
  codePointLength,
  describeActor,
  describeCandidateRejection,
  describeConfidence,
  describeLinkPlanStatus,
  describeOpportunityKind,
  describePlanAnchorSource,
  describeReasonCode,
  describeRetrievalChannel,
  describeScoreComponent,
  describeScoreFactor,
  outcomeToRejected,
  paramText,
  paramValue,
  parseMinScore,
  parseStatusFilter,
  parseUuidParam,
  planHref,
  sortStatusCounts,
  splitHighlight,
  topReasonCodes,
} from "@/lib/recommendations";

import { PAGE_ID } from "./fixtures/inventory";
import { ANCHOR, BLOCK_TEXT, linkPlan } from "./fixtures/recommendations";

type Schemas = components["schemas"];

// Every value of each contract enum, spelled out so that this test (as well as
// the Record types in lib/recommendations.ts) notices a contract change.
const STATUSES: Schemas["LinkPlanStatus"][] = [
  "proposed", "critiqued", "policy_checked", "awaiting_approval", "approved", "rejected", "superseded",
  "validated", "deployed", "verified", "failed", "rolled_back", "expired", "measured", "learning_captured",
];
const REASONS: Schemas["ReasonCode"][] = [
  "semantically_similar", "lexical_overlap", "same_market", "parent_market", "submarket", "related_market",
  "supply_chain", "complementary_market", "industry_adjacency", "same_geography", "regional_context",
  "high_commercial_value", "underlinked_target", "orphan_target", "anchor_matches_target_topic",
  "anchor_matches_target_title",
];
const REJECTIONS: Schemas["CandidateRejection"][] = [
  "self_link", "source_protected", "target_protected", "experiment_locked", "source_ineligible",
  "target_ineligible", "already_linked", "duplicate_recommendation", "previously_rejected",
  "page_type_incompatible", "below_min_relevance", "no_natural_anchor", "below_min_score",
  "link_density_exceeded", "per_source_limit", "diversity_limit",
];

describe("enum labels", () => {
  it("label every plan status with a distinct, known presentation", () => {
    expect([...LINK_PLAN_STATUSES]).toEqual(STATUSES);
    const labels = STATUSES.map((status) => describeLinkPlanStatus(status));
    expect(labels.every((p) => p.known && !p.label.startsWith("Unknown"))).toBe(true);
    expect(new Set(labels.map((p) => p.label)).size).toBe(STATUSES.length);
    // The lifecycle states a reviewer must never confuse.
    expect(describeLinkPlanStatus("proposed").label).toBe("Proposed");
    expect(describeLinkPlanStatus("awaiting_approval").label).toBe("Awaiting approval");
    expect(describeLinkPlanStatus("approved").label).toBe("Approved");
    expect(describeLinkPlanStatus("deployed").label).toBe("Deployed");
    expect(describeLinkPlanStatus("verified").label).toBe("Verified");
    expect(describeLinkPlanStatus("failed")).toMatchObject({ label: "Failed", tone: "critical" });
    expect(describeLinkPlanStatus("rolled_back").label).toBe("Rolled back");
    expect(describeLinkPlanStatus("approved").description).toMatch(/not deployed/i);
  });

  it("label every reason and rejection code, channel, anchor source and opportunity kind", () => {
    expect([...REASON_CODES]).toEqual(REASONS);
    expect([...CANDIDATE_REJECTIONS]).toEqual(REJECTIONS);
    expect([...OPPORTUNITY_KINDS]).toEqual(["orphan", "underlinked", "link_capacity"]);
    for (const code of REASONS) expect(describeReasonCode(code)).not.toMatch(/^Unknown/);
    for (const code of REJECTIONS) expect(describeCandidateRejection(code)).not.toMatch(/^Unknown/);
    for (const channel of ["semantic", "lexical", "taxonomy", "graph"]) {
      expect(describeRetrievalChannel(channel)).not.toMatch(/^Unknown/);
    }
    for (const source of ["target_topic", "target_title", "target_keyword", "edited"]) {
      expect(describePlanAnchorSource(source)).not.toMatch(/^Unknown/);
    }
    expect(describeReasonCode("orphan_target")).toBe("Target has no inbound links");
    expect(describeCandidateRejection("no_natural_anchor")).toBe("No natural anchor text in the source");
    expect(describeOpportunityKind("link_capacity")).toBe("Link capacity");
  });

  it("degrade unexpected values to an explicit Unknown label", () => {
    expect(describeLinkPlanStatus("teleported")).toMatchObject({ label: "Unknown (teleported)", known: false });
    expect(describeLinkPlanStatus(" ").label).toBe("Unknown (empty)");
    expect(describeReasonCode("vibes")).toBe("Unknown (vibes)");
    expect(describeCandidateRejection("x")).toBe("Unknown (x)");
    expect(describeOpportunityKind("y")).toBe("Unknown (y)");
    expect(describeRetrievalChannel("z")).toBe("Unknown (z)");
    // Inherited object keys are not labels.
    expect(describeReasonCode("toString")).toBe("Unknown (toString)");
  });

  it("names score components and factors, flagging unrecognised policy keys", () => {
    expect(describeScoreComponent("semantic")).toMatchObject({ label: "Semantic", known: true });
    expect(describeScoreComponent("historical").description).toMatch(/Phase 8/);
    expect(describeScoreComponent("brand_fit")).toEqual({ label: "Brand fit", description: null, known: false });
    expect(describeScoreFactor("placement_quality")).toMatchObject({ label: "Placement quality", known: true });
  });
});

describe("confidence", () => {
  it("labels uncalibrated confidence as raw and never as calibrated", () => {
    const raw = describeConfidence({ confidence: 0.6312, calibration_version: null });
    expect(raw).toMatchObject({ value: "0.631", calibrated: false, qualifier: "raw" });
    expect(raw.description).toMatch(/raw/i);
    expect(raw.description).toMatch(/not a probability/);
    expect(describeConfidence({ confidence: 0.5, calibration_version: "  " }).qualifier).toBe("raw");
  });

  it("labels a calibrated confidence with its calibration version", () => {
    const calibrated = describeConfidence({ confidence: 0.5, calibration_version: "isotonic-3" });
    expect(calibrated).toMatchObject({ calibrated: true, qualifier: "calibrated" });
    expect(calibrated.description).toContain("isotonic-3");
  });
});

describe("checkScore", () => {
  it("re-derives relevance and total from the record", () => {
    expect(checkScore(linkPlan().score)).toMatchObject({ relevanceHolds: true, totalHolds: true });
    const check = checkScore(linkPlan().score);
    expect(check.sumOfContributions).toBeCloseTo(0.7);
    expect(check.productOfFactors).toBeCloseTo(0.9);
  });

  it("flags a record whose numbers do not add up", () => {
    const score = linkPlan().score;
    expect(checkScore({ ...score, relevance: 0.8 }).relevanceHolds).toBe(false);
    expect(checkScore({ ...score, total: 0.7 }).totalHolds).toBe(false);
    expect(checkScore({ ...score, factors: {}, total: 0.7 })).toMatchObject({ productOfFactors: 1, totalHolds: true });
  });
});

describe("splitHighlight", () => {
  it("splits at the locator offsets when they select the anchor verbatim", () => {
    expect(splitHighlight(BLOCK_TEXT, 11, 37, ANCHOR)).toEqual({
      before: "The global ",
      match: ANCHOR,
      after: " is growing as electric vehicles scale.",
    });
  });

  it("refuses offsets that do not select the anchor, or are out of range", () => {
    expect(splitHighlight(BLOCK_TEXT, 10, 36, ANCHOR)).toBeNull();
    expect(splitHighlight(BLOCK_TEXT, 11, 37, ANCHOR.toUpperCase())).toBeNull();
    expect(splitHighlight(BLOCK_TEXT, -1, 5, "The g")).toBeNull();
    expect(splitHighlight(BLOCK_TEXT, 5, 5, "")).toBeNull();
    expect(splitHighlight(BLOCK_TEXT, 70, 500, "x")).toBeNull();
    expect(splitHighlight(BLOCK_TEXT, 1.5, 3, "he")).toBeNull();
  });

  it("counts offsets in code points, as the API does, not UTF-16 units", () => {
    const text = "🔋 EV battery market grows";
    // Python: text.index("EV") == 2 (the emoji is one code point, two UTF-16 units).
    expect(splitHighlight(text, 2, 19, "EV battery market")).toEqual({
      before: "🔋 ",
      match: "EV battery market",
      after: " grows",
    });
    expect(codePointLength(text)).toBe(25);
  });
});

describe("helpers", () => {
  it("shows the top reason codes and counts the rest", () => {
    expect(topReasonCodes(["a", "b", "c", "d", "e"])).toEqual({ shown: ["a", "b", "c"], hidden: 2 });
    expect(topReasonCodes(["a"])).toEqual({ shown: ["a"], hidden: 0 });
  });

  it("parses actors as text, without trusting unknown prefixes", () => {
    expect(describeActor("user:alice@example.com")).toEqual({ kind: "user", label: "User", id: "alice@example.com" });
    expect(describeActor("agent:critic@1.2.0")).toMatchObject({ kind: "agent", label: "Agent", id: "critic@1.2.0" });
    expect(describeActor("system:recommendation")).toMatchObject({ kind: "system", id: "recommendation" });
    expect(describeActor("<img src=x>")).toEqual({ kind: "unknown", label: "Unrecognised actor", id: "<img src=x>" });
    expect(describeActor("user:")).toMatchObject({ kind: "unknown" });
  });

  it("orders status counts by lifecycle, unknown statuses last", () => {
    expect(sortStatusCounts({ zeta: 1, rejected: 2, proposed: 5, approved: 1 }).map((row) => row.key)).toEqual([
      "proposed",
      "approved",
      "rejected",
      "zeta",
    ]);
  });

  it("builds encoded plan hrefs", () => {
    expect(planHref("a/b")).toBe("/recommendations/a%2Fb");
  });
});

describe("query parsing", () => {
  it("defaults the status filter to proposed and accepts all or a contract status", () => {
    expect(parseStatusFilter({})).toBe("proposed");
    expect(parseStatusFilter({ status: "all" })).toBe(ALL_STATUSES);
    expect(parseStatusFilter({ status: "approved" })).toBe("approved");
    expect(parseStatusFilter({ status: ["verified", "x"] })).toBe("verified");
    expect(parseStatusFilter({ status: "approved; drop" })).toBe("proposed");
  });

  it("accepts a minimum score only within [0, 1]", () => {
    expect(parseMinScore({})).toEqual({ kind: "absent" });
    expect(parseMinScore({ min_score: "0.5" })).toEqual({ kind: "ok", value: 0.5, raw: "0.5" });
    expect(paramValue(parseMinScore({ min_score: "1" }))).toBe(1);
    expect(paramValue(parseMinScore({ min_score: ".25" }))).toBe(0.25);
    expect(paramValue(parseMinScore({ min_score: "0" }))).toBe(0);
    for (const bad of ["1.5", "-0.1", "abc", "0.5e1", "NaN", "Infinity", "1.01"]) {
      expect(parseMinScore({ min_score: bad })).toMatchObject({ kind: "invalid" });
    }
    expect(paramText(parseMinScore({ min_score: "abc" }))).toBe("abc");
  });

  it("accepts UUID filters only", () => {
    expect(parseUuidParam({ source_page_id: PAGE_ID.toUpperCase() }, "source_page_id")).toEqual({
      kind: "ok",
      value: PAGE_ID,
      raw: PAGE_ID.toUpperCase(),
    });
    expect(parseUuidParam({ source_page_id: "../etc" }, "source_page_id")).toEqual({ kind: "invalid", raw: "../etc" });
    expect(paramValue(parseUuidParam({}, "source_page_id"))).toBeUndefined();
  });

  it("maps the candidate outcome filter to the contract's rejected flag", () => {
    expect(outcomeToRejected(undefined)).toBeUndefined();
    expect(outcomeToRejected("kept")).toBe(false);
    expect(outcomeToRejected("rejected")).toBe(true);
  });
});
