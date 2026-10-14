import type {
  CandidateView,
  LinkPlan,
  LinkPlanDetail,
  OpportunityView,
  PlanTransitionView,
  RecommendationSummary,
} from "@/lib/api-client";

import { OTHER_PAGE_ID, PAGE_ID } from "./inventory";

export const PLAN_ID = "44444444-4444-4444-8444-444444444444";
export const OLD_PLAN_ID = "55555555-5555-4555-8555-555555555555";
export const REC_RUN_ID = "66666666-6666-4666-8666-666666666666";

export const BLOCK_TEXT = "The global lithium-ion battery market is growing as electric vehicles scale.";
export const ANCHOR = "lithium-ion battery market";

export function linkPlan(overrides: Partial<LinkPlan> = {}): LinkPlan {
  return {
    id: PLAN_ID,
    status: "proposed",
    source_page_id: PAGE_ID,
    source_page_version: 2,
    source_url: "https://www.example.com/reports/widgets",
    target_page_id: OTHER_PAGE_ID,
    target_page_version: 5,
    target_url: "https://www.example.com/reports/lithium-ion-battery-market",
    target_title: "Lithium-ion battery market report",
    anchor_text: ANCHOR,
    anchor_source: "target_topic",
    relationship_type: "related_market",
    channels: ["semantic", "taxonomy"],
    reason_codes: ["related_market", "semantically_similar", "high_commercial_value", "anchor_matches_target_topic"],
    rationale: "The target covers a related market. The pages are semantically similar.",
    confidence: 0.63,
    calibration_version: null,
    score: {
      components: [
        { name: "semantic", value: 0.8, weight: 0.5, contribution: 0.4 },
        { name: "taxonomy", value: 1, weight: 0.3, contribution: 0.3 },
        { name: "historical", value: 0, weight: 0.2, contribution: 0 },
      ],
      factors: { placement_quality: 0.9 },
      policy_name: "ranking_weights",
      policy_version: 1,
      relevance: 0.7,
      semantic_floor: 0.35,
      total: 0.63,
    },
    locator: {
      block_id: "b4",
      block_kind: "p",
      occurrence_index: 0,
      start: 11,
      end: 37,
      context_hash: "a".repeat(64),
    },
    preview: {
      before: "The global ",
      anchor: ANCHOR,
      after: " is growing as electric vehicles scale.",
    },
    engine_version: "recommend-1.0.0",
    policy_version: 1,
    ranking_policy_version: 1,
    analysis_version: "analysis-2",
    embedding_model: "nomic-embed-text",
    reasoning_model: null,
    run_id: REC_RUN_ID,
    supersedes: null,
    created_at: "2026-10-14T08:00:00Z",
    updated_at: "2026-10-14T08:00:00Z",
    ...overrides,
  };
}

export function transition(overrides: Partial<PlanTransitionView> = {}): PlanTransitionView {
  return {
    id: 1,
    plan_id: PLAN_ID,
    from_status: null,
    to_status: "proposed",
    actor: "system:recommendation",
    note: null,
    occurred_at: "2026-10-14T08:00:00Z",
    ...overrides,
  };
}

export function linkPlanDetail(overrides: Partial<LinkPlanDetail> = {}): LinkPlanDetail {
  return {
    ...linkPlan(),
    source_title: "Widgets market report",
    source_block_text: BLOCK_TEXT,
    transitions: [transition()],
    ...overrides,
  };
}

export function candidate(overrides: Partial<CandidateView> = {}): CandidateView {
  return {
    run_id: REC_RUN_ID,
    source_page_id: PAGE_ID,
    target_page_id: OTHER_PAGE_ID,
    target_url: "https://www.example.com/reports/lithium-ion-battery-market",
    channels: ["semantic", "lexical"],
    channel_scores: { semantic: 0.81, lexical: 0.42 },
    total_score: 0.63,
    rejection: null,
    plan_id: PLAN_ID,
    ...overrides,
  };
}

export function opportunity(overrides: Partial<OpportunityView> = {}): OpportunityView {
  return {
    page_id: OTHER_PAGE_ID,
    canonical_url: "https://www.example.com/reports/lithium-ion-battery-market",
    title: "Lithium-ion battery market report",
    kind: "orphan",
    score: 0.9,
    evidence: { inbound_links: 0, commercial_importance: 3 },
    run_id: REC_RUN_ID,
    computed_at: "2026-10-14T08:00:00Z",
    ...overrides,
  };
}

export function recommendationSummary(overrides: Partial<RecommendationSummary> = {}): RecommendationSummary {
  return {
    plans_by_status: { proposed: 12, approved: 2, rejected: 1 },
    sources_with_plans: 6,
    targets_with_plans: 9,
    opportunities_by_kind: { orphan: 3, underlinked: 5, link_capacity: 7 },
    last_run_id: REC_RUN_ID,
    policy_version: 1,
    ranking_policy_version: 1,
    ...overrides,
  };
}
