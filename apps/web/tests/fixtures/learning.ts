import type {
  CorpusBanner,
  EvaluationSummary,
  LearningOverview,
  ProposalDetail,
  ProposalSummary,
  ProposalTransitionView,
  SkillDetail,
  SkillSummary,
} from "@/lib/api-client";

export const PROPOSAL_ID = "77777777-7777-4777-8777-777777777777";
export const SKILL_ID = "88888888-8888-4888-8888-888888888888";
export const EVAL_ID = "99999999-9999-4999-8999-999999999999";
export const REFLECTION_RUN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

/**
 * ADR-0016 §16.1: demonstration mode is ON by default, so a test that wants a
 * promotable system has to say so rather than getting one by accident.
 */
export function corpusBanner(overrides: Partial<CorpusBanner> = {}): CorpusBanner {
  return {
    demonstration_mode: true,
    note: "This corpus is 65 synthetic pages, 48 link plans and 12 review actions. No proposal on this data may be promoted.",
    pages_total: 65,
    plans_total: 48,
    review_actions_total: 12,
    min_sample_count: 30,
    ...overrides,
  };
}

export function learningOverview(overrides: Partial<LearningOverview> = {}): LearningOverview {
  return {
    banner: corpusBanner(),
    policy_version: 1,
    proposals_by_status: { proposed: 2, offline_test: 1 },
    skills_by_status: { promoted: 1, draft: 2 },
    evaluations_by_gate_result: { inconclusive: 3 },
    experiments_by_status: { draft: 1 },
    latest_reflection_run_id: REFLECTION_RUN_ID,
    latest_evaluation_run_id: EVAL_ID,
    ...overrides,
  };
}

export function proposalSummary(overrides: Partial<ProposalSummary> = {}): ProposalSummary {
  return {
    id: PROPOSAL_ID,
    kind: "policy",
    status: "proposed",
    title: "Raise the semantic floor to 0.40",
    target_kind: "ranking",
    base_version: "ranking.v1",
    fingerprint: "c".repeat(64),
    proposed_by: "agent:reflector@1",
    model: "qwen2.5:7b",
    fallback_used: false,
    job_run_id: REFLECTION_RUN_ID,
    supersedes: null,
    created_at: "2026-10-16T07:00:00Z",
    updated_at: "2026-10-16T07:30:00Z",
    ...overrides,
  };
}

export function evaluationSummary(overrides: Partial<EvaluationSummary> = {}): EvaluationSummary {
  return {
    id: EVAL_ID,
    mode: "offline",
    gate_result: "inconclusive",
    suites: ["destination-selection", "anchor-selection"],
    proposal_id: PROPOSAL_ID,
    candidate_id: null,
    skill_id: null,
    baseline_evaluation_id: null,
    proposal_fingerprint: "c".repeat(64),
    runner_version: "evals-1.0.0",
    model_provider: "ollama",
    git_sha: "0123456789abcdef",
    job_run_id: REFLECTION_RUN_ID,
    started_at: "2026-10-16T07:10:00Z",
    finished_at: "2026-10-16T07:20:00Z",
    ...overrides,
  };
}

export function skillSummary(overrides: Partial<SkillSummary> = {}): SkillSummary {
  return {
    id: SKILL_ID,
    name: "pick-anchor",
    version: 2,
    status: "promoted",
    source: "human",
    description: "Choose the anchor span inside a block.",
    proposal_id: null,
    evaluation_run_id: EVAL_ID,
    evaluation_score: 0.82,
    promoted_by: "user:jane",
    promoted_at: "2026-10-16T08:00:00Z",
    created_at: "2026-10-15T08:00:00Z",
    ...overrides,
  };
}

export function skillDetail(overrides: Partial<SkillDetail> = {}): SkillDetail {
  return {
    ...skillSummary(),
    instructions: "Pick the anchor span verbatim from the block text.",
    model_role: "fast",
    input_schema: { type: "object", properties: { block: { type: "string" } } },
    output_schema: { type: "object", properties: { start: { type: "integer" } } },
    suites: ["anchor-selection"],
    versions: [skillSummary(), skillSummary({ id: "b".repeat(32), version: 1, status: "retired" })],
    ...overrides,
  };
}

export function proposalTransition(overrides: Partial<ProposalTransitionView> = {}): ProposalTransitionView {
  return {
    id: 1,
    proposal_id: PROPOSAL_ID,
    from_status: "proposed",
    to_status: "offline_test",
    actor: "user:jane",
    reason: null,
    note: null,
    evaluation_run_id: EVAL_ID,
    created_at: "2026-10-16T07:30:00Z",
    ...overrides,
  };
}

export function proposalDetail(overrides: Partial<ProposalDetail> = {}): ProposalDetail {
  return {
    ...proposalSummary(),
    rationale: "Below 0.40 the kept candidates had the worst editorial agreement.",
    frozen_fields_untouched: true,
    evidence: {
      aggregate_version: "agg-1",
      analysis_version: "analysis-2",
      chosen_index: 0,
      engine_version: "reflect-1.0.0",
      fallback_reason: null,
      fallback_used: false,
      generated_at: "2026-10-16T07:00:00Z",
      model: "qwen2.5:7b",
      observations: [],
      options: [],
      policy_versions: { ranking: "ranking.v1" },
      sample_count_total: 12,
      window: { start: "2026-09-01", end: "2026-10-01" },
    },
    changes: [{ path: "ranking.semantic_floor", from_value: 0.35, to_value: 0.4 }],
    evaluations: [evaluationSummary()],
    required_stages: ["offline_test", "shadow"],
    stages_completed: ["offline_test"],
    candidate: null,
    skill: null,
    // Demonstration mode refuses the promotion: the console reports it.
    promotion: {
      blocked: true,
      blocked_by: ["demonstration_mode", "sample_too_small"],
      next_version: "ranking.v2",
      rollback_to_version: "ranking.v1",
      requires_operator_restart: true,
      target_paths: ["policies/ranking/ranking_weights.v2.yaml"],
    },
    transitions: [proposalTransition()],
    ...overrides,
  };
}
