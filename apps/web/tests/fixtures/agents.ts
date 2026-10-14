import type {
  AgentPlanReview,
  AgentRunDetail,
  AgentRunSummary,
  AgentStepView,
  ToolCallRecord,
} from "@/lib/api-client";

import { PAGE_ID } from "./inventory";
import { OLD_PLAN_ID, PLAN_ID } from "./recommendations";

export const AGENT_RUN_ID = "77777777-7777-4777-8777-777777777777";
export const AGENT_JOB_ID = "88888888-8888-4888-8888-888888888888";
export const CORRELATION_ID = "99999999-9999-4999-8999-999999999999";
export const TRACE_ID = "99999999999949998999999999999999";
export const SPAN_ID = "0123456789abcdef";

export function toolCall(overrides: Partial<ToolCallRecord> = {}): ToolCallRecord {
  return {
    tool: "get_page",
    permission: "read",
    ok: true,
    duration_ms: 12,
    attempt: 1,
    error_code: null,
    summary: { page_id: PAGE_ID, found: true },
    ...overrides,
  };
}

export function agentStep(overrides: Partial<AgentStepView> = {}): AgentStepView {
  return {
    run_id: AGENT_RUN_ID,
    seq: 0,
    node: "load_page",
    status: "succeeded",
    attempt: 1,
    started_at: "2026-10-14T10:00:00Z",
    finished_at: "2026-10-14T10:00:00.250Z",
    duration_ms: 250,
    model_role: null,
    model: null,
    fallback_used: false,
    fallback_reason: null,
    error_code: null,
    tool_calls: [toolCall()],
    output: { source_page_version: 2 },
    trace_id: TRACE_ID,
    span_id: SPAN_ID,
    ...overrides,
  };
}

export function agentPlanReview(overrides: Partial<AgentPlanReview> = {}): AgentPlanReview {
  return {
    run_id: AGENT_RUN_ID,
    plan_id: PLAN_ID,
    verdict: "accept",
    reasons: ["natural_anchor", "relevant_destination"],
    explanation: "The anchor reads naturally and the target covers the named market.",
    handed_off: true,
    fallback_used: false,
    fallback_reason: null,
    model: "qwen3:14b",
    reviewed_at: "2026-10-14T10:00:02Z",
    ...overrides,
  };
}

export function agentRunSummary(overrides: Partial<AgentRunSummary> = {}): AgentRunSummary {
  return {
    id: AGENT_RUN_ID,
    job_run_id: AGENT_JOB_ID,
    source_page_id: PAGE_ID,
    source_page_version: 2,
    source_url: "https://www.example.com/reports/widgets",
    trigger: "batch",
    status: "awaiting_approval",
    outcome: "recommend",
    triage_decision: "optimize",
    handed_off_plan_ids: [PLAN_ID],
    correlation_id: CORRELATION_ID,
    trace_id: TRACE_ID,
    graph_version: "graph-v1",
    started_at: "2026-10-14T10:00:00Z",
    finished_at: "2026-10-14T10:00:03.500Z",
    error_code: null,
    ...overrides,
  };
}

export function agentRunDetail(overrides: Partial<AgentRunDetail> = {}): AgentRunDetail {
  return {
    ...agentRunSummary(),
    steps: [
      agentStep(),
      agentStep({
        seq: 5,
        node: "critique",
        model_role: "reasoning",
        model: null,
        fallback_used: true,
        fallback_reason: "model_timeout",
        tool_calls: [],
        output: { reviewed: 2 },
      }),
      agentStep({
        seq: 7,
        node: "policy_gate",
        tool_calls: [toolCall({ tool: "hand_off_plans", permission: "review", duration_ms: 40 })],
        output: { handed_off: 1 },
      }),
    ],
    reviews: [
      agentPlanReview(),
      agentPlanReview({
        plan_id: OLD_PLAN_ID,
        verdict: "reject",
        reasons: ["weak_relevance"],
        explanation: "",
        handed_off: false,
        fallback_used: true,
        fallback_reason: "model_timeout",
        model: null,
      }),
    ],
    config_snapshot: { graph_version: "graph-v1", agents_policy_version: 1, critic: { use_model: true } },
    review_notes: [],
    ...overrides,
  };
}
