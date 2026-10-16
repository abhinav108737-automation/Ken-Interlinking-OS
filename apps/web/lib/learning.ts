/**
 * Presentation logic for the learning control plane (ADR-0016): policy and
 * skill versions, proposals, evaluation outcomes and the promotion gate.
 *
 * Portable and pure, with the conventions of lib/status.ts: every enum mapping
 * is a `Record` keyed by a generated contract enum, so a new contract value
 * fails type-checking until it is labelled, and an unexpected runtime value
 * degrades to an explicit "Unknown (…)" label instead of crashing.
 *
 * ADR-0016 §16.1 is a safety property, not styling: the corpus is small and
 * synthetic, `learning_demonstration_mode` defaults true, and NO proposal on
 * this data may be promoted. Every learning screen therefore carries a
 * persistent banner naming the corpus size and that rule, and the promotion
 * control reports the refusal rather than hiding it.
 */
import type {
  CorpusBanner,
  GateReasonCode,
  GateResult,
  ProposalKind,
  ProposalRejectionReason,
  ProposalStatus,
  PromotionRefusal,
  SkillSource,
  SkillStatus,
  TargetKind,
} from "./api-client";
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

function present<K extends string>(
  table: Readonly<Record<K, StatusPresentation>>,
  value: string,
  subject: string,
): StatusPresentation {
  if (hasOwn(table, value)) return table[value];
  return {
    label: unknownLabel(value),
    description: `The API reported a ${subject} this console does not recognise.`,
    tone: "neutral",
    known: false,
  };
}

// ---------------------------------------------------------------------------
// 1. The corpus banner (ADR-0016 §16.1). Non-dismissible, on every learning
//    screen. `note` is a FIXED sentence from the API and never model output, so
//    it is rendered verbatim as text; the counts are shown beside it so the
//    claim is checkable.
// ---------------------------------------------------------------------------

export const CORPUS_BANNER_TITLE = "Demonstration data — no proposal here may be promoted";

export interface CorpusBannerPresentation {
  readonly title: string;
  /** The API's fixed sentence. Rendered as text, never as HTML. */
  readonly note: string;
  /** `pages / plans / review actions` counts, as display rows. */
  readonly counts: readonly (readonly [string, number])[];
  readonly demonstrationMode: boolean;
  /** The sentence that explains what demonstration mode mechanically does. */
  readonly enforcement: string;
  readonly minSampleCount: number;
}

export function describeCorpusBanner(banner: CorpusBanner): CorpusBannerPresentation {
  return {
    title: CORPUS_BANNER_TITLE,
    note: banner.note,
    counts: [
      ["Pages", banner.pages_total],
      ["Link plans", banner.plans_total],
      ["Review actions", banner.review_actions_total],
    ],
    demonstrationMode: banner.demonstration_mode,
    enforcement: banner.demonstration_mode
      ? "Demonstration mode is ON: the API refuses to promote anything, by construction, not by convention. Every number on these screens is a demonstration, not evidence."
      : "Demonstration mode is OFF, so a promotion can succeed. Check the corpus size below before you trust any number here as evidence.",
    minSampleCount: banner.min_sample_count,
  };
}

/** True when a gate cannot pass on this little data, whatever it measured. */
export function belowSampleFloor(cases: number, banner: Pick<CorpusBanner, "min_sample_count">): boolean {
  return cases < banner.min_sample_count;
}

// ---------------------------------------------------------------------------
// 2. Proposals
// ---------------------------------------------------------------------------

const PROPOSAL_KIND: Readonly<Record<ProposalKind, string>> = {
  policy: "Policy",
  skill: "Skill",
};

export const describeProposalKind = (value: string) => label(PROPOSAL_KIND, value);

const PROPOSAL_STATUS: Readonly<Record<ProposalStatus, StatusPresentation>> = {
  proposed: {
    label: "Proposed",
    description: "Generated from recorded experience. Nothing has changed in the system's behaviour.",
    tone: "neutral",
    known: true,
  },
  offline_test: {
    label: "Offline tested",
    description: "Replayed against the evaluation corpus in memory and scored against the committed baseline.",
    tone: "neutral",
    known: true,
  },
  shadow: {
    label: "Shadow",
    description: "Evaluated alongside the active document without affecting any real behaviour.",
    tone: "caution",
    known: true,
  },
  canary: {
    label: "Canary",
    description: "Applied to a limited slice, under observation.",
    tone: "caution",
    known: true,
  },
  experiment: {
    label: "Experiment",
    description: "Running as an assigned experiment arm.",
    tone: "caution",
    known: true,
  },
  promoted: {
    label: "Promoted",
    description: "Promoted into the active behaviour by a human. Never by an agent, and never automatically.",
    tone: "positive",
    known: true,
  },
  rejected: {
    label: "Rejected",
    description: "Refused with a structured reason. Terminal.",
    tone: "neutral",
    known: true,
  },
  rolled_back: {
    label: "Rolled back",
    description: "Promoted and then reverted to the previous version.",
    tone: "caution",
    known: true,
  },
  withdrawn: {
    label: "Withdrawn",
    description: "Taken out of consideration before any gate. Terminal.",
    tone: "neutral",
    known: true,
  },
};

export const PROPOSAL_STATUSES = Object.keys(PROPOSAL_STATUS) as readonly ProposalStatus[];
export const describeProposalStatus = (value: string) => present(PROPOSAL_STATUS, value, "proposal status");

const PROPOSAL_REJECTION_REASON: Readonly<Record<ProposalRejectionReason, string>> = {
  insufficient_evidence: "Not enough evidence",
  gate_failed: "An evaluation gate failed",
  frozen_field: "It touches a frozen field",
  out_of_scope: "Out of scope",
  risk_too_high: "Risk too high",
  superseded_by_newer: "Superseded by a newer proposal",
  demonstration_only: "Demonstration data only",
  regression_observed: "A regression was observed",
  operational_incident: "An operational incident",
};

export const PROPOSAL_REJECTION_REASONS = Object.keys(
  PROPOSAL_REJECTION_REASON,
) as readonly ProposalRejectionReason[];
export const describeProposalRejectionReason = (value: string) => label(PROPOSAL_REJECTION_REASON, value);

const TARGET_KIND: Readonly<Record<TargetKind, string>> = {
  ranking: "Ranking weights",
  recommendation: "Recommendation policy",
  agents: "Agent policy",
};

export const describeTargetKind = (value: string) => label(TARGET_KIND, value);

// ---------------------------------------------------------------------------
// 3. Skills
// ---------------------------------------------------------------------------

const SKILL_STATUS: Readonly<Record<SkillStatus, StatusPresentation>> = {
  draft: {
    label: "Draft",
    description: "Authored or proposed, not evaluated and not in use.",
    tone: "neutral",
    known: true,
  },
  offline_test: {
    label: "Offline tested",
    description: "Scored against the evaluation suites. Not in use.",
    tone: "neutral",
    known: true,
  },
  promoted: {
    label: "Promoted",
    description: "In use. Promoted by a human, with an evaluation reference.",
    tone: "positive",
    known: true,
  },
  rejected: {
    label: "Rejected",
    description: "Refused. Terminal.",
    tone: "neutral",
    known: true,
  },
  retired: {
    label: "Retired",
    description: "Was in use and has been withdrawn. Kept for attribution.",
    tone: "neutral",
    known: true,
  },
};

export const SKILL_STATUSES = Object.keys(SKILL_STATUS) as readonly SkillStatus[];
export const describeSkillStatus = (value: string) => present(SKILL_STATUS, value, "skill status");

const SKILL_SOURCE: Readonly<Record<SkillSource, string>> = {
  human: "Authored by a person",
  reflection: "Proposed by the reflection job",
};

export const describeSkillSource = (value: string) => label(SKILL_SOURCE, value);

// ---------------------------------------------------------------------------
// 4. Evaluation outcomes and the gate
// ---------------------------------------------------------------------------

const GATE_RESULT: Readonly<Record<GateResult, StatusPresentation>> = {
  pass: {
    label: "Pass",
    description: "Every required suite met its threshold and nothing regressed against the baseline.",
    tone: "positive",
    known: true,
  },
  fail: {
    label: "Fail",
    description: "At least one gate was violated. The reasons are listed with the run.",
    tone: "critical",
    known: true,
  },
  inconclusive: {
    label: "Inconclusive",
    description:
      "The gate could not reach a verdict — most often too few cases per decision class. Inconclusive is never treated as a pass.",
    tone: "caution",
    known: true,
  },
};

export const GATE_RESULTS = Object.keys(GATE_RESULT) as readonly GateResult[];
export const describeGateResult = (value: string) => present(GATE_RESULT, value, "gate result");

const GATE_REASON_CODE: Readonly<Record<GateReasonCode, string>> = {
  hard_fail_suite: "A hard-fail suite did not pass",
  must_not_violation: "A must-not rule was violated",
  below_min_pass_rate: "Below the minimum pass rate",
  regression_vs_baseline: "A regression against the baseline",
  suite_not_run: "A required suite was not run",
  sample_too_small: "Too few cases to decide",
  behavioural_signal_negative: "A behavioural signal was negative",
  evidence_not_reproducible: "The evidence could not be reproduced",
  frozen_field_touched: "A frozen field was touched",
  baseline_missing: "No baseline to compare against",
};

export const describeGateReasonCode = (value: string) => label(GATE_REASON_CODE, value);

const PROMOTION_REFUSAL: Readonly<Record<PromotionRefusal, string>> = {
  demonstration_mode: "Demonstration mode is on: nothing may be promoted on this data.",
  not_a_human: "Only a human may promote. An agent never can.",
  permission_denied: "The role lacks the promote permission.",
  transition_not_allowed: "That status transition is not allowed from here.",
  required_stage_missing: "A required stage has not been completed.",
  evaluation_missing: "No evaluation run was referenced.",
  evaluation_not_found: "The referenced evaluation run does not exist.",
  evaluation_not_passed: "The referenced evaluation did not pass its gate.",
  evaluation_foreign: "The evaluation belongs to a different proposal.",
  fingerprint_mismatch: "The proposal changed after it was evaluated.",
  dataset_changed: "The evaluation dataset changed after the run.",
  evaluation_too_old: "The evaluation is too old to rely on.",
  sample_too_small: "Too few samples to support a promotion.",
  behavioural_block: "A behavioural signal blocks this promotion. Behavioural data may only block, never justify.",
  frozen_field: "The candidate changes a frozen field.",
  reason_required: "A structured reason is required.",
  replayed_with_different_body: "This operation id was already used with a different request.",
  promoter_guard_missing: "The promotion guard is not in place.",
};

export const describePromotionRefusal = (value: string) => label(PROMOTION_REFUSAL, value);

/** The headline sentence for a promotion preview: what is blocking it, if anything. */
export function describePromotionBlock(blockedBy: readonly string[]): StatusPresentation {
  if (blockedBy.length === 0) {
    return {
      label: "No refusal recorded",
      description: "Nothing in the promotion preview refuses this proposal. The API still re-checks on submission.",
      tone: "neutral",
      known: true,
    };
  }
  return {
    label: blockedBy.length === 1 ? "Promotion refused" : `Promotion refused (${blockedBy.length} reasons)`,
    description: blockedBy.map(describePromotionRefusal).join(" "),
    tone: "critical",
    known: true,
  };
}

const EVALUATION_MODE: Readonly<Record<string, string>> = {
  offline: "Offline",
  shadow: "Shadow",
  canary: "Canary",
};

export const describeEvaluationMode = (value: string) => label(EVALUATION_MODE, value);

/** Suite ids are kebab-case in the contract; shown humanised, never hidden. */
export function describeEvalSuite(value: string): string {
  const trimmed = value.trim();
  if (trimmed === "") return unknownLabel(value);
  return trimmed
    .split("-")
    .map((part, index) => (index === 0 ? part.charAt(0).toUpperCase() + part.slice(1) : part))
    .join(" ");
}

// ---------------------------------------------------------------------------
// 5. Routes and query handling
// ---------------------------------------------------------------------------

export const LEARNING_PATH = "/learning";
export const PROPOSALS_PATH = "/learning/proposals";
export const SKILLS_PATH = "/learning/skills";
export const EVALUATIONS_PATH = "/learning/evaluations";

export function proposalHref(proposalId: string): string {
  return `${PROPOSALS_PATH}/${encodeURIComponent(proposalId)}`;
}

export function skillHref(name: string): string {
  return `${SKILLS_PATH}/${encodeURIComponent(name)}`;
}

/** `status`: a contract proposal status, or nothing. An unrecognised value is ignored. */
export function parseProposalStatus(params: SearchParams, name = "status"): ProposalStatus | undefined {
  const value = firstParam(params, name);
  return value !== undefined && (PROPOSAL_STATUSES as readonly string[]).includes(value)
    ? (value as ProposalStatus)
    : undefined;
}

/** `kind`: `policy` or `skill`, or nothing. */
export function parseProposalKind(params: SearchParams, name = "kind"): ProposalKind | undefined {
  const value = firstParam(params, name);
  return value === "policy" || value === "skill" ? value : undefined;
}

/** `status`: a contract skill status, or nothing. */
export function parseSkillStatus(params: SearchParams, name = "status"): SkillStatus | undefined {
  const value = firstParam(params, name);
  return value !== undefined && (SKILL_STATUSES as readonly string[]).includes(value)
    ? (value as SkillStatus)
    : undefined;
}

/** `gate_result`: a contract gate result, or nothing. */
export function parseGateResult(params: SearchParams, name = "gate_result"): GateResult | undefined {
  const value = firstParam(params, name);
  return value !== undefined && (GATE_RESULTS as readonly string[]).includes(value)
    ? (value as GateResult)
    : undefined;
}

// ---------------------------------------------------------------------------
// 6. Advancing a proposal (`POST /learning/proposals/{id}/advance`, `promote`).
//
// This is the governed human control-plane action of ADR-0018 §5: an agent may
// propose, only a person promotes. Three properties are deliberate:
//
//  - `demonstration_mode` refuses `promoted` outright, server-side. The console
//    REPORTS that refusal; it never hides the control to pretend the rule does
//    not exist, and it never claims a refusal was a success.
//  - a structured `reason` is required to reject, so a rejection is auditable.
//  - the `operation_id` is minted once per rendered form and re-minted only
//    after a recorded outcome, so a retry after a timeout replays.
// ---------------------------------------------------------------------------

/** The contract's `NoteText` bound. */
export const MAX_NOTE_CHARS = 500;

export type AdvanceFieldErrors = Readonly<Partial<Record<"to_status" | "reason" | "note" | "evaluation_run_id", string>>>;

export interface AdvanceInput {
  readonly toStatus: string;
  readonly reason: string;
  readonly note: string;
  readonly evaluationRunId: string;
}

/** Statuses whose transition requires a structured rejection reason. */
const REASON_REQUIRED: ReadonlySet<string> = new Set(["rejected", "rolled_back", "withdrawn"]);

export function reasonRequiredFor(toStatus: string): boolean {
  return REASON_REQUIRED.has(toStatus);
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Re-validate the advance form before sending it. The API validates again;
 * this exists so an operator gets a field-level message rather than a 422.
 */
export function validateAdvanceInput(input: AdvanceInput): AdvanceFieldErrors {
  const errors: Record<string, string> = {};
  if (!(PROPOSAL_STATUSES as readonly string[]).includes(input.toStatus)) {
    errors["to_status"] = "Choose the status to move this proposal to.";
  }
  if (reasonRequiredFor(input.toStatus) && !(PROPOSAL_REJECTION_REASONS as readonly string[]).includes(input.reason)) {
    errors["reason"] = "A structured reason is required for this transition.";
  }
  if (input.note.length > MAX_NOTE_CHARS) {
    errors["note"] = `A note must be ${MAX_NOTE_CHARS} characters or fewer.`;
  }
  if (input.evaluationRunId.trim() !== "" && !UUID.test(input.evaluationRunId.trim())) {
    errors["evaluation_run_id"] = "An evaluation run id must be a UUID.";
  }
  return errors;
}

export function hasAdvanceErrors(errors: AdvanceFieldErrors): boolean {
  return Object.keys(errors).length > 0;
}

export type AdvanceState =
  | { readonly status: "idle" }
  | { readonly status: "invalid"; readonly message: string; readonly fieldErrors: AdvanceFieldErrors }
  | { readonly status: "error"; readonly message: string; readonly outcomeUnknown: boolean; readonly code?: string }
  | { readonly status: "done"; readonly message: string; readonly replayed: boolean; readonly toStatus: string };

export const ADVANCE_IDLE: AdvanceState = { status: "idle" };

export function describeAdvanceOutcome(toStatus: string, replayed: boolean): AdvanceState {
  const label = describeProposalStatus(toStatus).label;
  return {
    status: "done",
    message: replayed
      ? `This repeated an earlier identical request; nothing changed twice. The proposal is ${label}.`
      : `The proposal is now ${label}. Recorded against your account.`,
    replayed,
    toStatus,
  };
}

const ADVANCE_ERROR_BY_STATUS: Readonly<Record<number, string>> = {
  403: "Your role may not advance a learning proposal. This needs the promote permission.",
  404: "This proposal no longer exists.",
  409: "That transition is not allowed from the proposal's current status, or a gate refused it.",
  422: "The request was rejected as invalid.",
};

/**
 * The operator-facing message for a failed advance. A 409 very often IS the
 * demonstration-mode refusal, so the message points at the banner rather than
 * sounding like a transient failure.
 */
export function advanceErrorMessage(error: {
  readonly kind: string;
  readonly status?: number;
  readonly message: string;
  readonly detail?: string;
}): string {
  if (error.kind === "timeout" || error.status === 503) {
    return (
      "The outcome of this request is UNKNOWN. It may have been recorded. " +
      "Reload this page and check the proposal's status before trying again."
    );
  }
  const byStatus = error.status === undefined ? undefined : ADVANCE_ERROR_BY_STATUS[error.status];
  const base = byStatus ?? `${error.message} Nothing was changed.`;
  return error.detail === undefined ? base : `${base} ${error.detail}`;
}

export function advanceOutcomeUnknown(error: { readonly kind: string; readonly status?: number }): boolean {
  if (error.kind === "timeout") return true;
  return error.status === 503;
}

/** True when the submission reached a recorded outcome, so the next one needs a fresh operation id. */
export function concludesAdvance(state: AdvanceState): boolean {
  if (state.status === "done") return true;
  return (
    state.status === "error" &&
    !state.outcomeUnknown &&
    (state.code === "conflict" || state.code === "validation_failed")
  );
}
