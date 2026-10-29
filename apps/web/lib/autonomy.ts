/**
 * Presentation logic for the autonomy-first operations control plane
 * (ADR-0018). This module is the reason the console is a monitoring and
 * exception-handling surface rather than an approval inbox.
 *
 * Portable and pure, with the conventions of lib/status.ts and
 * lib/recommendations.ts: every enum mapping is a `Record` keyed by a generated
 * contract enum, so a new contract value fails type-checking until it is
 * labelled, and an unexpected runtime value degrades to an explicit
 * "Unknown (…)" label instead of crashing.
 *
 * Types come from `lib/api-client.ts`, never from the env-reading `lib/api.ts`.
 *
 * TWO rules in here are safety properties, not styling:
 *
 * 1. In `shadow` mode nothing is published. A shadow decision is a
 *    COUNTERFACTUAL and must never be legible as an action taken, so every
 *    shadow row carries its own label (`describeShadowDecision`) and the mode
 *    is announced on every relevant screen (`describeAutonomyMode`).
 * 2. A deterministic block is never overridable into a publish. The override
 *    form says so, and a 200 response with `applied: false` and a non-empty
 *    `blocks` is a REFUSAL, which `describeOverrideOutcome` reports as such.
 */
import type {
  ApiError,
  AutonomyDecision,
  AutonomyDecisionView,
  AutonomyHealth,
  AutonomyMode,
  BlockCode,
  BlockedActionView,
  CriticOutcome,
  LinkPlanStatus,
  OperatorOverrideResponse,
  OverrideAction,
  PublishAttemptView,
  RiskTier,
  VerificationProfile,
} from "./api-client";
import { type SearchParams, buildHref, firstParam } from "./inventory";
import { LINK_PLAN_STATUSES } from "./recommendations";
import type { StatusPresentation, Tone } from "./status";

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
// 1. Autonomy mode (ADR-0018 §2). Shown on EVERY relevant screen, because in
//    `shadow` nothing is really published and a reader must never mistake a
//    counterfactual for an action taken.
// ---------------------------------------------------------------------------

const AUTONOMY_MODE: Readonly<Record<AutonomyMode, StatusPresentation>> = {
  shadow: {
    label: "Shadow — nothing is published",
    description:
      "The autonomy engine evaluates and records the decision it WOULD have taken. No link is written to the site. Every decision recorded in this mode is a counterfactual, not an action that happened.",
    tone: "caution",
    known: true,
  },
  guarded: {
    label: "Guarded — low-risk publishing only",
    description:
      "Low-risk, high-confidence auto-publish decisions are published without a human. Everything else is blocked or deferred to an operator as an exception.",
    tone: "caution",
    known: true,
  },
  autonomous: {
    label: "Autonomous — publishing without approval",
    description:
      "Every policy-eligible decision is published without a human. Deterministic safety rules still apply and every publish is verified.",
    tone: "critical",
    known: true,
  },
};

export const AUTONOMY_MODES = Object.keys(AUTONOMY_MODE) as readonly AutonomyMode[];

export function describeAutonomyMode(value: string): StatusPresentation {
  return present(AUTONOMY_MODE, value, "autonomy mode");
}

/** Short form for a table cell or a badge beside a row. */
const AUTONOMY_MODE_SHORT: Readonly<Record<AutonomyMode, string>> = {
  shadow: "Shadow",
  guarded: "Guarded",
  autonomous: "Autonomous",
};

export const describeAutonomyModeShort = (value: string) => label(AUTONOMY_MODE_SHORT, value);

/** True for the mode in which nothing is published, so every decision is a counterfactual. */
export function isShadowMode(mode: string): boolean {
  return mode === "shadow";
}

/**
 * The one sentence that answers "can this system change the site right now?".
 * It is derived from the API's own `publishing_enabled`, never recomputed here:
 * the mode alone does not decide it (the CMS provider and the acknowledgement
 * flags do too), and the console must not disagree with the backend.
 */
export function describePublishing(health: Pick<AutonomyHealth, "mode" | "publishing_enabled">): StatusPresentation {
  if (!health.publishing_enabled) {
    return {
      label: "Publishing disabled",
      description: isShadowMode(health.mode)
        ? "Nothing can be written to the site in shadow mode. Decisions are recorded as counterfactuals only."
        : "The mode, the acknowledgement or the CMS configuration does not permit a real write, so nothing is published.",
      tone: "positive",
      known: true,
    };
  }
  return {
    label: "Publishing enabled",
    description:
      "The mode, the acknowledgement and the CMS all permit a real write. Links can be published to the site without a human approving them.",
    tone: "critical",
    known: true,
  };
}

// ---------------------------------------------------------------------------
// 2. The autonomy decision (ADR-0018 §1). Exactly three outcomes, and there is
//    no "approve" among them.
// ---------------------------------------------------------------------------

const AUTONOMY_DECISION: Readonly<Record<AutonomyDecision, StatusPresentation>> = {
  block: {
    label: "Block",
    description:
      "Never published. A deterministic hard block or a policy refusal. An operator may override a policy refusal, with a recorded reason; a deterministic block is never overridable.",
    tone: "critical",
    known: true,
  },
  auto_publish: {
    label: "Auto-publish",
    description: "Published without a human, then verified with the standard profile.",
    tone: "positive",
    known: true,
  },
  auto_publish_with_extra_verification: {
    label: "Auto-publish (extra verification)",
    description:
      "Published without a human, then verified with the strict profile: an HTTP re-fetch, a second pass after a delay and a stricter mutation-isolation check.",
    tone: "positive",
    known: true,
  },
};

export const AUTONOMY_DECISIONS = Object.keys(AUTONOMY_DECISION) as readonly AutonomyDecision[];

export function describeAutonomyDecision(value: string): StatusPresentation {
  return present(AUTONOMY_DECISION, value, "autonomy decision");
}

/** True when the decision is one of the two publishing decisions. */
export function isPublishDecision(decision: string): boolean {
  return decision === "auto_publish" || decision === "auto_publish_with_extra_verification";
}

// ---------------------------------------------------------------------------
// 3. Shadow labelling. The single most important rule in this module: a
//    decision recorded in `shadow` never happened.
// ---------------------------------------------------------------------------

export interface ShadowPresentation {
  /** True when this ROW was recorded in shadow, whatever the mode is now. */
  readonly shadow: boolean;
  /** Unmistakable short label for the row. */
  readonly label: string;
  /** Full sentence naming the counterfactual, for a title or a screen reader. */
  readonly description: string;
  /** The decision that WOULD have been taken, labelled, when the API recorded one. */
  readonly wouldHave: string | null;
}

/**
 * How to label one recorded decision. `mode` is the mode that was in force when
 * the row was written, not the mode now, so a historical shadow row stays
 * labelled as a counterfactual after the operator moves to `autonomous`.
 */
export function describeShadowDecision(
  view: Pick<AutonomyDecisionView, "mode" | "decision" | "would_have_decision">,
): ShadowPresentation {
  if (!isShadowMode(view.mode)) {
    return {
      shadow: false,
      label: "Recorded and acted on",
      description: `Evaluated in ${describeAutonomyModeShort(view.mode).toLowerCase()} mode, where the decision is carried out.`,
      wouldHave: null,
    };
  }
  const wouldHave =
    view.would_have_decision === null ? null : describeAutonomyDecision(view.would_have_decision).label;
  return {
    shadow: true,
    label: "Shadow — did not happen",
    description:
      wouldHave === null
        ? "Recorded in shadow mode: nothing was published and nothing on the site changed. This row is a counterfactual."
        : `Recorded in shadow mode: nothing was published and nothing on the site changed. Had the mode been autonomous, the decision would have been “${wouldHave}”.`,
    wouldHave,
  };
}

/** The badge for a shadow row. Paired with the label, never colour alone. */
export const SHADOW_BADGE: StatusPresentation = {
  label: "Shadow — did not happen",
  description:
    "Recorded while the autonomy mode was shadow. Nothing was published and nothing on the site changed. This is a counterfactual record.",
  tone: "caution",
  known: true,
};

// ---------------------------------------------------------------------------
// 4. Deterministic hard blocks (completion directive §9). The CLOSED set, each
//    with what an operator should look at. A block is computed from stored
//    facts by code alone: no model call, no judgement, and never overridable
//    into a publish.
// ---------------------------------------------------------------------------

interface BlockPresentation {
  readonly label: string;
  /** What fired, in one sentence. */
  readonly description: string;
  /** What an operator should check to resolve it. */
  readonly investigate: string;
}

const BLOCK_CODE: Readonly<Record<BlockCode, BlockPresentation>> = {
  self_link: {
    label: "Self link",
    description: "The destination is the source page itself.",
    investigate: "A recommendation defect. Check the candidate retrieval for the source page.",
  },
  external_destination: {
    label: "External destination",
    description: "The destination is not an in-scope internal page.",
    investigate: "Check the crawl scope and the destination's canonical URL.",
  },
  protected_page: {
    label: "Protected page",
    description: "The source or the destination is protected, so no mutation is allowed.",
    investigate: "Check the page's protections on its intelligence screen, and who declared them.",
  },
  unpublished_destination: {
    label: "Unpublished destination",
    description: "The destination is not published, so a link to it would be broken for readers.",
    investigate: "Check the destination's publication state in the inventory.",
  },
  broken_target: {
    label: "Broken destination",
    description: "The destination did not resolve when it was last checked.",
    investigate: "Re-crawl the destination and check the latest ingestion issues for it.",
  },
  stale_source_version: {
    label: "Stale source version",
    description: "The source page changed after the plan was made, so the anchor offsets may no longer be valid.",
    investigate: "Re-run the recommendation for this source page; the plan itself is stale and should expire.",
  },
  duplicate_contextual_link: {
    label: "Duplicate contextual link",
    description: "The source already links to the destination from its body text.",
    investigate: "Check the source page's existing body links.",
  },
  prohibited_page_type: {
    label: "Prohibited page type",
    description: "The page type or intent of the source or destination forbids the link.",
    investigate: "Check the taxonomy page types and the recommendation policy's compatibility rules.",
  },
  policy_disallowed_destination: {
    label: "Destination disallowed by policy",
    description: "The active policy excludes this destination as a link target.",
    investigate: "Check the destination exclusions in the active recommendation policy.",
  },
  invalid_canonical: {
    label: "Invalid canonical URL",
    description: "The destination's canonical URL could not be used as a link href.",
    investigate: "Check the destination's canonical status in the inventory.",
  },
  unsupported_redirect: {
    label: "Unsupported redirect",
    description: "The destination redirects in a way the system will not link through.",
    investigate: "Re-crawl the destination and link to its final URL instead.",
  },
  invalid_page_state: {
    label: "Invalid page state",
    description: "The source or destination page is in a state that forbids a mutation (for example, gone).",
    investigate: "Check both pages in the inventory for their current state.",
  },
  invalid_anchor: {
    label: "Invalid anchor",
    description: "The proposed anchor text no longer matches the source block verbatim.",
    investigate: "Compare the plan's anchor against the source block text on the plan's screen.",
  },
  missing_rollback_metadata: {
    label: "Rollback not possible",
    description:
      "An inverse splice is not possible for this plan, so publishing it could not be undone. Nothing is published that cannot be reversed.",
    investigate: "Check the source page's stored HTML and version; the plan needs regenerating against the current version.",
  },
  stale_vector: {
    label: "Stale vector",
    description:
      "The semantic hit that produced this plan was built from a superseded page version or a different content hash.",
    investigate: "Re-index the vector store (`make qdrant-index`) and check `make qdrant-verify` for drift.",
  },
};

export const BLOCK_CODES = Object.keys(BLOCK_CODE) as readonly BlockCode[];

export function describeBlockCode(value: string): string {
  return hasOwn(BLOCK_CODE, value) ? BLOCK_CODE[value].label : unknownLabel(value);
}

export function describeBlockCodeDetail(value: string): string {
  return hasOwn(BLOCK_CODE, value)
    ? BLOCK_CODE[value].description
    : "A deterministic block code this console does not recognise. The API still enforces it.";
}

export function describeBlockInvestigation(value: string): string | null {
  return hasOwn(BLOCK_CODE, value) ? BLOCK_CODE[value].investigate : null;
}

/** True for a code this build knows, so an unknown code can be flagged rather than hidden. */
export function isKnownBlockCode(value: string): value is BlockCode {
  return hasOwn(BLOCK_CODE, value);
}

/** Block codes in contract order; codes this build does not know come last, by key. */
export function sortBlockCounts(counts: Readonly<Record<string, number>>): { key: string; count: number }[] {
  const rank = (key: string) => {
    const index = (BLOCK_CODES as readonly string[]).indexOf(key);
    return index === -1 ? BLOCK_CODES.length : index;
  };
  return Object.entries(counts)
    .map(([key, count]) => ({ key, count }))
    .sort((a, b) => rank(a.key) - rank(b.key) || a.key.localeCompare(b.key, "en"));
}

// ---------------------------------------------------------------------------
// 5. Risk tier, verification profile, critic outcome
// ---------------------------------------------------------------------------

const RISK_TIER: Readonly<Record<RiskTier, StatusPresentation>> = {
  low: {
    label: "Low risk",
    description: "Eligible for auto-publishing in guarded mode when the confidence is calibrated and high.",
    tone: "positive",
    known: true,
  },
  medium: {
    label: "Medium risk",
    description: "Published only in autonomous mode; guarded mode defers or blocks it.",
    tone: "caution",
    known: true,
  },
  high: {
    label: "High risk",
    description:
      "Raised by an uncertain critic verdict or a risky placement. Published only in autonomous mode, with extra verification.",
    tone: "critical",
    known: true,
  },
};

export const RISK_TIERS = Object.keys(RISK_TIER) as readonly RiskTier[];
export const describeRiskTier = (value: string) => present(RISK_TIER, value, "risk tier");

const VERIFICATION_PROFILE: Readonly<Record<VerificationProfile, { label: string; description: string }>> = {
  standard: {
    label: "Standard",
    description: "The usual post-publish checks: the link is present, isolated and resolves.",
  },
  strict: {
    label: "Strict",
    description:
      "An HTTP re-fetch, a second pass after a delay and a stricter mutation-isolation check. Only the profile differs; the safety rules never do.",
  },
};

export const VERIFICATION_PROFILES = Object.keys(VERIFICATION_PROFILE) as readonly VerificationProfile[];
export const describeVerificationProfile = (value: string) =>
  hasOwn(VERIFICATION_PROFILE, value) ? VERIFICATION_PROFILE[value].label : unknownLabel(value);
export const describeVerificationProfileDetail = (value: string): string | null =>
  hasOwn(VERIFICATION_PROFILE, value) ? VERIFICATION_PROFILE[value].description : null;

const CRITIC_OUTCOME: Readonly<Record<CriticOutcome, StatusPresentation>> = {
  accept: { label: "Accept", description: "The critic step accepted the plan.", tone: "positive", known: true },
  reject: { label: "Reject", description: "The critic step rejected the plan.", tone: "critical", known: true },
  no_action: {
    label: "No action",
    description:
      "A separate, earlier outcome: the plan is not a defect, there is simply nothing worth doing. It never becomes a publish.",
    tone: "neutral",
    known: true,
  },
};

export const describeCriticOutcome = (value: string) => present(CRITIC_OUTCOME, value, "critic outcome");

/**
 * Confidence with its calibration qualifier. A decision without a
 * `calibration_version` was taken on RAW confidence, which is not a
 * probability and must never be presented as one (ADR-0011 §4).
 */
export function describeDecisionConfidence(
  view: Pick<AutonomyDecisionView, "confidence" | "calibration_version">,
): { readonly value: string; readonly qualifier: "raw" | "calibrated"; readonly description: string } {
  const version = view.calibration_version?.trim() ?? "";
  const value = Number.isFinite(view.confidence) ? view.confidence.toFixed(3) : "—";
  if (version === "") {
    return {
      value,
      qualifier: "raw",
      description: "Raw confidence: the uncalibrated score. It is not a probability.",
    };
  }
  return { value, qualifier: "calibrated", description: `Calibrated confidence (calibration ${version}).` };
}

// ---------------------------------------------------------------------------
// 6. THE OPERATIONAL STATES (ADR-0018 §3). These seven, not an approval queue,
//    are the vocabulary the console speaks in. Each `LinkPlanStatus` belongs to
//    exactly one of them, and the approval pair is collected under `exception`
//    so it reads as what it now is.
// ---------------------------------------------------------------------------

export type OperationalState =
  | "proposed"
  | "blocked"
  | "auto_published"
  | "verification_pending"
  | "verified"
  | "rolled_back"
  | "failed"
  | "exception"
  | "closed";

interface OperationalStatePresentation {
  readonly label: string;
  readonly description: string;
  readonly tone: Tone;
  /** True for the seven primary states the product is organised around. */
  readonly primary: boolean;
  /** The plan statuses that land in this state. */
  readonly statuses: readonly LinkPlanStatus[];
}

const OPERATIONAL_STATE: Readonly<Record<OperationalState, OperationalStatePresentation>> = {
  proposed: {
    label: "Proposed",
    description: "Generated and being evaluated. Nothing has been written to the site.",
    tone: "neutral",
    primary: true,
    statuses: ["proposed", "critiqued", "policy_checked", "autonomy_decided"],
  },
  blocked: {
    label: "Blocked",
    description:
      "Refused by a deterministic safety rule. Never published unless an operator overrides it, which records who and why.",
    tone: "critical",
    primary: true,
    statuses: ["blocked"],
  },
  auto_published: {
    label: "Auto-published",
    description: "Written to the site by the autonomy engine, without a human approving it.",
    tone: "positive",
    primary: true,
    statuses: ["validated"],
  },
  verification_pending: {
    label: "Verification pending",
    description: "On the page, but verification has not confirmed it yet.",
    tone: "caution",
    primary: true,
    statuses: ["deployed"],
  },
  verified: {
    label: "Verified",
    description: "Confirmed present and isolated on the live page.",
    tone: "positive",
    primary: true,
    statuses: ["verified", "measured", "learning_captured"],
  },
  rolled_back: {
    label: "Rolled back",
    description: "The published link was removed again, by the system on a verification failure or by an operator.",
    tone: "caution",
    primary: true,
    statuses: ["rolled_back"],
  },
  failed: {
    label: "Failed",
    description: "Publishing or verification failed. Needs investigation.",
    tone: "critical",
    primary: true,
    statuses: ["failed"],
  },
  exception: {
    label: "Operator exception",
    description:
      "Deferred to or acted on by an operator. Not the normal path: normal link publishing needs no approval (ADR-0018).",
    tone: "caution",
    primary: false,
    statuses: ["awaiting_approval", "approved"],
  },
  closed: {
    label: "Closed",
    description: "Will not be published: rejected, superseded by an edit, or expired because the source changed.",
    tone: "neutral",
    primary: false,
    statuses: ["rejected", "superseded", "expired"],
  },
};

/** The seven primary states, in lifecycle order. */
export const PRIMARY_OPERATIONAL_STATES: readonly OperationalState[] = [
  "proposed",
  "blocked",
  "auto_published",
  "verification_pending",
  "verified",
  "rolled_back",
  "failed",
];

/** Every state, primary first, then the two non-primary groupings. */
export const OPERATIONAL_STATES: readonly OperationalState[] = [
  ...PRIMARY_OPERATIONAL_STATES,
  "exception",
  "closed",
];

const STATE_OF_STATUS: ReadonlyMap<string, OperationalState> = new Map(
  OPERATIONAL_STATES.flatMap((state) =>
    OPERATIONAL_STATE[state].statuses.map((status) => [status as string, state] as const),
  ),
);

/** The operational state a plan status belongs to, or `null` for a status this build does not know. */
export function operationalStateOf(status: string): OperationalState | null {
  return STATE_OF_STATUS.get(status) ?? null;
}

export function describeOperationalState(state: OperationalState): StatusPresentation {
  const entry = OPERATIONAL_STATE[state];
  return { label: entry.label, description: entry.description, tone: entry.tone, known: true };
}

export function statusesOf(state: OperationalState): readonly LinkPlanStatus[] {
  return OPERATIONAL_STATE[state].statuses;
}

export function isPrimaryState(state: OperationalState): boolean {
  return OPERATIONAL_STATE[state].primary;
}

export interface OperationalStateCount {
  readonly state: OperationalState;
  readonly count: number;
  readonly presentation: StatusPresentation;
  /** The contract statuses rolled into this count, so the number is auditable. */
  readonly statuses: readonly LinkPlanStatus[];
}

/**
 * Roll a `plans_by_status` map up into the operational states, in lifecycle
 * order. Statuses this build does not know are returned separately rather than
 * silently dropped: a count that is not shown anywhere is a lie about the total.
 */
export function rollUpStates(counts: Readonly<Record<string, number>>): {
  readonly states: readonly OperationalStateCount[];
  readonly unknown: readonly { readonly key: string; readonly count: number }[];
  readonly total: number;
} {
  const totals = new Map<OperationalState, number>(OPERATIONAL_STATES.map((state) => [state, 0]));
  const unknown: { key: string; count: number }[] = [];
  let total = 0;
  for (const [key, count] of Object.entries(counts)) {
    total += count;
    const state = operationalStateOf(key);
    if (state === null) {
      unknown.push({ key, count });
      continue;
    }
    totals.set(state, (totals.get(state) ?? 0) + count);
  }
  return {
    states: OPERATIONAL_STATES.map((state) => ({
      state,
      count: totals.get(state) ?? 0,
      presentation: describeOperationalState(state),
      statuses: statusesOf(state),
    })),
    unknown: unknown.sort((a, b) => a.key.localeCompare(b.key, "en")),
    total,
  };
}

// ---------------------------------------------------------------------------
// 7. Routes
// ---------------------------------------------------------------------------

export const AUTONOMY_PATH = "/autonomy";
export const DECISIONS_PATH = "/autonomy/decisions";
export const BLOCKED_PATH = "/autonomy/blocked";
export const ATTEMPTS_PATH = "/autonomy/attempts";

/** The investigation view for one blocked plan. */
export function blockedPlanHref(planId: string): string {
  return `${BLOCKED_PATH}/${encodeURIComponent(planId)}`;
}

/** The blocked list filtered to one code. */
export function blockedCodeHref(code: string): string {
  return buildHref(BLOCKED_PATH, { code });
}

/** The decision log filtered to one plan. */
export function planDecisionsHref(planId: string): string {
  return buildHref(DECISIONS_PATH, { plan_id: planId });
}

/** The publish-attempt ledger filtered to one plan. */
export function planAttemptsHref(planId: string): string {
  return buildHref(ATTEMPTS_PATH, { plan_id: planId });
}

// ---------------------------------------------------------------------------
// 8. Query-string handling (GET forms only; nothing here mutates)
// ---------------------------------------------------------------------------

/** `decision`: a contract decision, or nothing. An unrecognised value is ignored. */
export function parseDecisionFilter(params: SearchParams, name = "decision"): AutonomyDecision | undefined {
  const value = firstParam(params, name);
  return value !== undefined && (AUTONOMY_DECISIONS as readonly string[]).includes(value)
    ? (value as AutonomyDecision)
    : undefined;
}

/** `mode`: a contract mode, or nothing. An unrecognised value is ignored. */
export function parseModeFilter(params: SearchParams, name = "mode"): AutonomyMode | undefined {
  const value = firstParam(params, name);
  return value !== undefined && (AUTONOMY_MODES as readonly string[]).includes(value)
    ? (value as AutonomyMode)
    : undefined;
}

/** `code`: a contract block code, or nothing. An unrecognised value is ignored. */
export function parseBlockCodeFilter(params: SearchParams, name = "code"): BlockCode | undefined {
  const value = firstParam(params, name);
  return value !== undefined && isKnownBlockCode(value) ? value : undefined;
}

// ---------------------------------------------------------------------------
// 9. The operator override (ADR-0018 §5). Under the `deploy` permission, with a
//    recorded reason, implemented as a server action.
// ---------------------------------------------------------------------------

const OVERRIDE_ACTION: Readonly<Record<OverrideAction, { label: string; description: string }>> = {
  publish: {
    label: "Publish anyway",
    description:
      "Publish this plan despite the recorded decision. A DETERMINISTIC block is never overridable: if one is still firing, the API refuses and records the refusal.",
  },
  block: {
    label: "Block",
    description: "Refuse this plan permanently. It will not be published and will not be retried.",
  },
};

export const OVERRIDE_ACTIONS = Object.keys(OVERRIDE_ACTION) as readonly OverrideAction[];
export const describeOverrideAction = (value: string) =>
  hasOwn(OVERRIDE_ACTION, value) ? OVERRIDE_ACTION[value].label : unknownLabel(value);
export const describeOverrideActionDetail = (value: string): string | null =>
  hasOwn(OVERRIDE_ACTION, value) ? OVERRIDE_ACTION[value].description : null;

/** The contract's `reason` bounds (`minLength: 1`, `maxLength: 500`). */
export const MIN_REASON_CHARS = 1;
export const MAX_REASON_CHARS = 500;

export type OverrideFieldErrors = Readonly<Partial<Record<"action" | "reason" | "expected_status", string>>>;

export interface OverrideInput {
  readonly action: string;
  readonly reason: string;
  readonly expectedStatus: string;
}

/**
 * Re-validate the override form before sending it. The API validates again;
 * this exists so the operator gets a field-level message instead of a 422, and
 * so a reason is never silently empty — an unexplained override is not
 * auditable.
 */
export function validateOverrideInput(input: OverrideInput): OverrideFieldErrors {
  const errors: Record<string, string> = {};
  if (!(OVERRIDE_ACTIONS as readonly string[]).includes(input.action)) {
    errors["action"] = "Choose whether to publish or block this plan.";
  }
  const reason = input.reason.trim();
  if (reason.length < MIN_REASON_CHARS) {
    errors["reason"] = "A reason is required: it is recorded with your name in the audit log.";
  } else if (input.reason.length > MAX_REASON_CHARS) {
    errors["reason"] = `A reason must be ${MAX_REASON_CHARS} characters or fewer.`;
  }
  // The status must be one the contract knows: a privileged mutation never
  // sends a value this build cannot name, even though the API validates again.
  if (!(LINK_PLAN_STATUSES as readonly string[]).includes(input.expectedStatus)) {
    errors["expected_status"] = "This form is out of date. Reload the page and try again.";
  }
  return errors;
}

export function hasOverrideErrors(errors: OverrideFieldErrors): boolean {
  return Object.keys(errors).length > 0;
}

/** The state of an override form, in the shape `useActionState` wants. */
export type OverrideState =
  | { readonly status: "idle" }
  | { readonly status: "invalid"; readonly message: string; readonly fieldErrors: OverrideFieldErrors }
  | {
      readonly status: "error";
      readonly message: string;
      readonly outcomeUnknown: boolean;
      readonly code?: string;
    }
  | {
      readonly status: "done";
      readonly message: string;
      /** False for a RECORDED REFUSAL: the override was not applied. */
      readonly applied: boolean;
      readonly blocks: readonly string[];
      readonly planStatus: string;
    };

export const OVERRIDE_IDLE: OverrideState = { status: "idle" };

/**
 * Turn a 200 response into the operator's message. A 200 with
 * `applied: false` is a REFUSAL, not a success: the console says so and names
 * the blocks that are still firing, because a deterministic block is never
 * overridable into a publish.
 */
export function describeOverrideOutcome(response: OperatorOverrideResponse): OverrideState {
  const action = describeOverrideAction(response.action);
  if (response.applied) {
    return {
      status: "done",
      message: `“${action}” applied. The plan is now ${response.plan_status}. Recorded against your account with the reason you gave.`,
      applied: true,
      blocks: response.blocks,
      planStatus: response.plan_status,
    };
  }
  const refused = response.refusal?.trim();
  const blocks =
    response.blocks.length === 0
      ? ""
      : ` Still firing: ${response.blocks.map(describeBlockCode).join(", ")}.`;
  return {
    status: "done",
    message: `“${action}” was REFUSED and nothing changed.${refused === undefined || refused === "" ? "" : ` ${refused}`}${blocks} A deterministic block is never overridable.`,
    applied: false,
    blocks: response.blocks,
    planStatus: response.plan_status,
  };
}

/**
 * True when the outcome of the request is UNKNOWN: the intent may have been
 * recorded durably and the CMS result may or may not have landed. The Phase 6
 * lesson: a 503 or a timeout must never be reported as "nothing changed".
 */
export function overrideOutcomeUnknown(error: ApiError): boolean {
  if (error.kind === "timeout") return true;
  return (error.kind === "api_error" || error.kind === "http_status") && error.status === 503;
}

const OVERRIDE_ERROR_BY_STATUS: Readonly<Record<number, string>> = {
  403: "Your role may not override an autonomy decision. This needs the deploy permission.",
  404: "This plan no longer exists.",
  409: "The plan is no longer in the status this form was built from, so the override was refused. Reload the page to see its current state.",
  422: "The override request was rejected as invalid.",
};

/** The operator-facing message for a failed override. Never claims nothing changed when it cannot know. */
export function overrideErrorMessage(error: ApiError): string {
  if (overrideOutcomeUnknown(error)) {
    return (
      "The outcome of this override is UNKNOWN. The request may have been recorded and the site may already have changed. " +
      "Do not retry with a new reason: reload this page and check the plan's state and its publish attempts first."
    );
  }
  const byStatus = error.status === undefined ? undefined : OVERRIDE_ERROR_BY_STATUS[error.status];
  if (byStatus !== undefined) {
    return error.detail === undefined ? byStatus : `${byStatus} ${error.detail}`;
  }
  return `${error.message} Nothing was changed.`;
}

/**
 * True when the submission reached a RECORDED outcome, so the next submission
 * is a new operation and needs a fresh idempotency key. A failure whose outcome
 * is unknown deliberately does NOT conclude: its retry must replay the same
 * operation id rather than apply a second time.
 */
export function concludesOverride(state: OverrideState): boolean {
  if (state.status === "done") return true;
  // A refusal the API recorded against the operation id (409 / 422) is final for
  // that id: replaying it returns the same refusal, so a corrected attempt needs
  // a new one.
  return state.status === "error" && !state.outcomeUnknown && (state.code === "conflict" || state.code === "validation_failed");
}

// ---------------------------------------------------------------------------
// 10. Publish attempts (ADR-0018 §4): the auto-published / rolled-back ledger.
// ---------------------------------------------------------------------------

/** The outcome of one recorded publish attempt, as a badge. */
export function describeAttemptOutcome(
  attempt: Pick<PublishAttemptView, "mode" | "succeeded" | "rolled_back" | "failure_code">,
): StatusPresentation {
  if (isShadowMode(attempt.mode)) {
    return {
      label: "Shadow — did not happen",
      description:
        "Recorded while the mode was shadow: this attempt was evaluated, not performed. Nothing on the site changed.",
      tone: "caution",
      known: true,
    };
  }
  if (attempt.rolled_back) {
    return {
      label: "Rolled back",
      description: "Published and then removed again. The pair is marked so the same plan is not retried in a loop.",
      tone: "caution",
      known: true,
    };
  }
  if (attempt.succeeded) {
    return { label: "Published", description: "Written to the site and not rolled back.", tone: "positive", known: true };
  }
  return {
    label: "Failed",
    description:
      attempt.failure_code === null
        ? "The attempt did not succeed. No failure code was recorded."
        : `The attempt did not succeed (${attempt.failure_code}).`,
    tone: "critical",
    known: true,
  };
}

/** True while the (source, target) pair is in its retry cool-down at `nowMs`. */
export function inCooldown(attempt: Pick<PublishAttemptView, "cooldown_until">, nowMs: number = Date.now()): boolean {
  if (attempt.cooldown_until === null) return false;
  const until = Date.parse(attempt.cooldown_until);
  return Number.isFinite(until) && until > nowMs;
}

// ---------------------------------------------------------------------------
// 11. Blocked-action helpers
// ---------------------------------------------------------------------------

/**
 * Whether a blocked plan is still open for an operator to act on. An already
 * overridden block is shown, but its override form is not offered again.
 */
export function isOpenBlock(view: Pick<BlockedActionView, "overridden">): boolean {
  return !view.overridden;
}

/** The highest-severity presentation among the codes that fired, for a row badge. */
export function describeBlockSet(blocks: readonly string[]): StatusPresentation {
  if (blocks.length === 0) {
    return {
      label: "No block recorded",
      description: "The API reported this plan as blocked but recorded no code. Treat it as unexplained and investigate.",
      tone: "neutral",
      known: false,
    };
  }
  const unknownCodes = blocks.filter((code) => !isKnownBlockCode(code));
  const labels = blocks.map(describeBlockCode).join(", ");
  return {
    label: blocks.length === 1 ? describeBlockCode(blocks[0] as string) : `${blocks.length} blocks`,
    description:
      unknownCodes.length === 0
        ? `Deterministic blocks that fired: ${labels}. A deterministic block is never overridable into a publish.`
        : `Deterministic blocks that fired: ${labels}. A deterministic block is never overridable into a publish. ${unknownCodes.length} of these codes is not known to this console build.`,
    tone: "critical",
    known: unknownCodes.length === 0,
  };
}
