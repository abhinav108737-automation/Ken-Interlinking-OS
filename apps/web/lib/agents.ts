/**
 * Presentation logic for the Phase 4 agent-run screens (ADR-0012).
 *
 * Portable and pure, with the conventions of lib/status.ts and
 * lib/recommendations.ts: every enum mapping is a `Record` keyed by a generated
 * contract enum, so a new contract value fails type-checking until it is
 * labelled, and an unexpected runtime value degrades to an explicit
 * "Unknown (…)" label instead of crashing.
 *
 * Read-only: agents REVIEW deterministic plans and hand some to the human
 * queue; nothing here starts a run, approves, rejects or edits a plan.
 *
 * The fetchers themselves live where every other screen's do: the portable
 * core in `lib/api-client.ts` (`listAgentRuns`, `getAgentRun`,
 * `listAgentPlanReviews`) and the env-reading server wrappers in `lib/api.ts`.
 * Types come from `lib/api-client.ts`, never from `lib/api.ts`.
 */
import type {
  AgentErrorCode,
  AgentNode,
  AgentOutcome,
  AgentRunStatus,
  AgentRunSummary,
  AgentTrigger,
  CriticReason,
  CriticVerdict,
  FallbackReason,
  ModelRole,
  StepStatus,
  ToolName,
  ToolPermission,
  TriageDecision,
} from "./api-client";
import { NONE, durationBetween, formatConfigValue, formatDuration } from "./format";
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

function present<K extends string>(
  table: Readonly<Record<K, StatusPresentation>>,
  value: string,
  subject: string,
): StatusPresentation {
  if (hasOwn(table, value)) return table[value];
  return {
    label: unknownLabel(value),
    description: `The API reported ${subject} this console does not recognise.`,
    tone: "neutral",
    known: false,
  };
}

// ---------------------------------------------------------------------------
// Run status and outcome. A run's `awaiting_approval` means it HANDED OFF plans
// to the human queue and ended; it is never "approved".
// ---------------------------------------------------------------------------

const AGENT_RUN_STATUS: Readonly<Record<AgentRunStatus, StatusPresentation>> = {
  running: {
    label: "Running",
    description: "The run has not finished (or its process died and it has not been reaped yet).",
    tone: "caution",
    known: true,
  },
  awaiting_approval: {
    label: "Awaiting approval",
    description:
      "The run ended by handing plans to the human review queue. Nothing is approved or deployed until a person approves it.",
    tone: "caution",
    known: true,
  },
  completed: {
    label: "Completed",
    description: "The run ended without handing off a plan.",
    tone: "neutral",
    known: true,
  },
  failed: {
    label: "Failed",
    description: "The run stopped with a typed error. No plan was changed by the failing step.",
    tone: "critical",
    known: true,
  },
};

const AGENT_OUTCOME: Readonly<Record<AgentOutcome, StatusPresentation>> = {
  recommend: {
    label: "Recommend",
    description: "At least one plan the critic accepted was handed to the human review queue.",
    tone: "positive",
    known: true,
  },
  no_action: {
    label: "No action",
    description: "Nothing to review: the source is ineligible, needs re-analysis, or has no unreviewed proposed plan.",
    tone: "neutral",
    known: true,
  },
  needs_human_review: {
    label: "Needs human review",
    description: "Only uncertain plans were handed off, or an invariant problem was found. See the review notes.",
    tone: "caution",
    known: true,
  },
  failed: {
    label: "Failed",
    description: "The run failed; see its error code.",
    tone: "critical",
    known: true,
  },
};

/** Shown when a run has no outcome yet (it is still running). */
export const NO_OUTCOME: StatusPresentation = {
  label: "Pending",
  description: "The run has not finished, so it has no outcome yet.",
  tone: "neutral",
  known: true,
};

const STEP_STATUS: Readonly<Record<StepStatus, StatusPresentation>> = {
  succeeded: { label: "Succeeded", description: "The node ran to completion.", tone: "positive", known: true },
  skipped: {
    label: "Skipped",
    description: "The node did not apply (for example, nothing to critique).",
    tone: "neutral",
    known: true,
  },
  failed: {
    label: "Failed",
    description: "A recoverable failure, handled by routing: the run continued or ended cleanly.",
    tone: "critical",
    known: true,
  },
};

const CRITIC_VERDICT: Readonly<Record<CriticVerdict, StatusPresentation>> = {
  accept: {
    label: "Accept",
    description: "The critic accepted the plan. A person must still approve it.",
    tone: "positive",
    known: true,
  },
  reject: {
    label: "Reject",
    description: "The critic did not hand the plan off. It stays proposed; only a person can reject a plan.",
    tone: "neutral",
    known: true,
  },
  uncertain: {
    label: "Uncertain",
    description: "Handed off with the critic's note: a person must look closely.",
    tone: "caution",
    known: true,
  },
};

/** Shown next to a step or review whose model output was replaced by the deterministic rule. */
export const FALLBACK_PRESENTATION: StatusPresentation = {
  label: "Fallback",
  description: "The deterministic rule was used instead of the model.",
  tone: "caution",
  known: true,
};

// ---------------------------------------------------------------------------
// Plain labels
// ---------------------------------------------------------------------------

const AGENT_TRIGGER_LABEL: Readonly<Record<AgentTrigger, string>> = {
  manual: "Manual (named pages)",
  batch: "Batch",
};

const TRIAGE_DECISION_LABEL: Readonly<Record<TriageDecision, string>> = {
  no_action: "No action",
  reindex: "Re-index (analysis missing or stale)",
  optimize: "Optimize (review proposed plans)",
  repair_link: "Repair link (reserved)",
  review_performance: "Review performance (reserved)",
};

/** Graph order (ADR-0012 section 2). */
const AGENT_NODE_LABEL: Readonly<Record<AgentNode, string>> = {
  load_page: "Load page",
  triage: "Triage",
  understand: "Understand",
  discover: "Discover",
  plan: "Plan",
  critique: "Critique",
  check_invariants: "Check invariants",
  policy_gate: "Policy gate",
  finalize: "Finalize",
};

const FALLBACK_REASON_LABEL: Readonly<Record<FallbackReason, string>> = {
  model_disabled: "Model disabled by policy",
  model_unavailable: "Model unavailable",
  model_timeout: "Model timed out",
  schema_violation: "Model output failed validation",
};

const AGENT_ERROR_CODE_LABEL: Readonly<Record<AgentErrorCode, string>> = {
  page_not_found: "Source page not found",
  tool_failed: "A tool call failed",
  timeout: "Timed out",
  invariant_violated: "A plan invariant was violated",
  handoff_conflict: "Hand-off conflict (a plan changed status concurrently)",
  orphaned: "Orphaned (the process stopped while running)",
  internal: "Internal error",
};

const CRITIC_REASON_LABEL: Readonly<Record<CriticReason, string>> = {
  natural_anchor: "Natural anchor text",
  relevant_destination: "Relevant destination",
  misleading_anchor: "Misleading anchor text",
  weak_relevance: "Weak relevance",
  reader_journey_mismatch: "Does not fit the reader's journey",
  over_linking: "Over-linking",
  low_confidence: "Low confidence",
};

const TOOL_NAME_LABEL: Readonly<Record<ToolName, string>> = {
  get_page: "Get page",
  get_analysis: "Get analysis",
  get_eligibility: "Get eligibility",
  list_proposed_plans: "List proposed plans",
  check_plan: "Check plan",
  hand_off_plans: "Hand off plans",
};

const TOOL_PERMISSION_LABEL: Readonly<Record<ToolPermission, string>> = {
  read: "Read",
  review: "Review (hands plans to the human queue)",
};

const MODEL_ROLE_LABEL: Readonly<Record<ModelRole, string>> = {
  fast: "Fast",
  reasoning: "Reasoning",
  embedding: "Embedding",
};

export const AGENT_RUN_STATUSES = Object.keys(AGENT_RUN_STATUS) as readonly AgentRunStatus[];
export const AGENT_OUTCOMES = Object.keys(AGENT_OUTCOME) as readonly AgentOutcome[];
export const AGENT_TRIGGERS = Object.keys(AGENT_TRIGGER_LABEL) as readonly AgentTrigger[];
export const TRIAGE_DECISIONS = Object.keys(TRIAGE_DECISION_LABEL) as readonly TriageDecision[];
export const AGENT_NODES = Object.keys(AGENT_NODE_LABEL) as readonly AgentNode[];
export const STEP_STATUSES = Object.keys(STEP_STATUS) as readonly StepStatus[];
export const FALLBACK_REASONS = Object.keys(FALLBACK_REASON_LABEL) as readonly FallbackReason[];
export const AGENT_ERROR_CODES = Object.keys(AGENT_ERROR_CODE_LABEL) as readonly AgentErrorCode[];
export const CRITIC_VERDICTS = Object.keys(CRITIC_VERDICT) as readonly CriticVerdict[];
export const CRITIC_REASONS = Object.keys(CRITIC_REASON_LABEL) as readonly CriticReason[];
export const TOOL_NAMES = Object.keys(TOOL_NAME_LABEL) as readonly ToolName[];
export const TOOL_PERMISSIONS = Object.keys(TOOL_PERMISSION_LABEL) as readonly ToolPermission[];
export const MODEL_ROLES = Object.keys(MODEL_ROLE_LABEL) as readonly ModelRole[];

export const describeAgentRunStatus = (value: string) => present(AGENT_RUN_STATUS, value, "an agent run status");
export const describeAgentOutcome = (value: string | null) =>
  value === null ? NO_OUTCOME : present(AGENT_OUTCOME, value, "an agent outcome");
export const describeStepStatus = (value: string) => present(STEP_STATUS, value, "a step status");
export const describeCriticVerdict = (value: string) => present(CRITIC_VERDICT, value, "a critic verdict");
export const describeAgentTrigger = (value: string) => label(AGENT_TRIGGER_LABEL, value);
export const describeTriageDecision = (value: string) => label(TRIAGE_DECISION_LABEL, value);
export const describeAgentNode = (value: string) => label(AGENT_NODE_LABEL, value);
export const describeFallbackReason = (value: string) => label(FALLBACK_REASON_LABEL, value);
export const describeAgentErrorCode = (value: string) => label(AGENT_ERROR_CODE_LABEL, value);
export const describeCriticReason = (value: string) => label(CRITIC_REASON_LABEL, value);
export const describeToolName = (value: string) => label(TOOL_NAME_LABEL, value);
export const describeToolPermission = (value: string) => label(TOOL_PERMISSION_LABEL, value);
export const describeModelRole = (value: string) => label(MODEL_ROLE_LABEL, value);

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

/** A run's duration, "In progress" while it runs, or `NONE` when it cannot be computed. */
export function agentRunDuration(run: Pick<AgentRunSummary, "started_at" | "finished_at" | "status">): string {
  const ms = durationBetween(run.started_at, run.finished_at);
  if (ms !== null) return formatDuration(ms);
  return run.finished_at === null && run.status === "running" ? "In progress" : NONE;
}

/**
 * A step's output or a tool call's summary as compact JSON TEXT, with
 * credential-looking keys withheld (defence in depth; the contract says both
 * are secret-free and bounded). Rendered inside a `<pre>` as a text node.
 */
export function formatAgentJson(value: Readonly<Record<string, unknown>>): string {
  return Object.keys(value).length === 0 ? "{}" : formatConfigValue(value);
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

export const AGENTS_PATH = "/agents";

export function agentRunHref(runId: string): string {
  return `${AGENTS_PATH}/${encodeURIComponent(runId)}`;
}
