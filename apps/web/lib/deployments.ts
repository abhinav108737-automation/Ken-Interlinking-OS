/**
 * Controlled deployment: presentation and form logic (ADR-0014 sections 7-8).
 *
 * Portable and pure (no React, no fetch). Conventions of lib/status.ts and lib/review.ts:
 * every enum mapping is a `Record` keyed by a generated contract enum, so a new contract
 * value fails type-checking until it is labelled, and an unexpected runtime value
 * degrades to an explicit "Unknown (…)" label. The rule tables mirror the FROZEN contract
 * `ken_contracts.deployment`; the API re-validates every request and is the authority.
 */
import type {
  ApiError,
  ChangePreview,
  DeploymentDetail,
  DeploymentIssueCode,
  DeploymentStatus,
  DryRunResult,
  RollbackReason,
  VerificationCheckName,
  VerificationOutcome,
} from "./api-client";
import { isUuid, splitHighlight, type HighlightParts } from "./recommendations";
import type { StatusPresentation } from "./status";

function hasOwn<T extends object>(record: T, key: string): key is Extract<keyof T, string> {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function unknownLabel(value: string): string {
  return `Unknown (${value.trim() === "" ? "empty" : value})`;
}

function unknownPresentation(value: string, subject: string): StatusPresentation {
  return {
    label: unknownLabel(value),
    description: `The API reported a ${subject} this console does not recognise.`,
    tone: "neutral",
    known: false,
  };
}

// ---------------------------------------------------------------------------
// Contract limits (ken_contracts.deployment)
// ---------------------------------------------------------------------------

export const MAX_PLANS_PER_DEPLOYMENT = 20;
export const MAX_DEPLOY_NOTE_CHARS = 500;

export const DEPLOYMENTS_PATH = "/deployments";

export function deploymentHref(deploymentId: string): string {
  return `${DEPLOYMENTS_PATH}/${encodeURIComponent(deploymentId)}`;
}

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

const DEPLOYMENT_STATUS: Readonly<Record<DeploymentStatus, StatusPresentation>> = {
  applying: {
    label: "Applying",
    description:
      "The deployment intent is recorded and the CMS call is in flight, or its outcome is not yet known. It is reconciled, never guessed.",
    tone: "caution",
    known: true,
  },
  applied: {
    label: "Applied",
    description: "The CMS accepted the change. Verification has not confirmed it yet.",
    tone: "positive",
    known: true,
  },
  verified: {
    label: "Verified",
    description: "Applied, and every link was confirmed on the page.",
    tone: "positive",
    known: true,
  },
  verification_failed: {
    label: "Verification failed",
    description: "Applied, but at least one verification check failed. Re-verify or roll back.",
    tone: "critical",
    known: true,
  },
  failed: {
    label: "Failed",
    description: "The CMS refused the change. Nothing was applied. Terminal.",
    tone: "critical",
    known: true,
  },
  conflict: {
    label: "Version conflict",
    description: "The CMS page changed before the change was applied. Nothing was applied; the plans expired. Terminal.",
    tone: "critical",
    known: true,
  },
  rolling_back: {
    label: "Rolling back",
    description: "The rollback intent is recorded and the CMS call is in flight, or its outcome is not yet known.",
    tone: "caution",
    known: true,
  },
  rolled_back: {
    label: "Rolled back",
    description: "The deployed links were removed from the page again. Terminal.",
    tone: "neutral",
    known: true,
  },
};

interface Labelled {
  readonly label: string;
  readonly description: string;
}

const DEPLOYMENT_ISSUE: Readonly<Record<DeploymentIssueCode, Labelled>> = {
  plan_not_approved: { label: "Not approved", description: "Only approved plans can be deployed." },
  plan_stale: { label: "Plan is stale", description: "The source page changed since the plan was made." },
  plan_invalid: {
    label: "Plan no longer valid",
    description: "A page is now protected or ineligible, or the target is already linked.",
  },
  mixed_sources: { label: "Different source pages", description: "One deployment covers one source page only." },
  cms_page_not_found: { label: "CMS page not found", description: "The CMS has no page for this source." },
  cms_page_protected: { label: "CMS page protected", description: "The CMS protects this page from changes." },
  cms_unavailable: { label: "CMS unavailable", description: "The CMS could not be reached." },
  cms_validation_failed: { label: "CMS refused the change", description: "The CMS's own validation rejected it." },
  href_invalid: { label: "Link address not allowed", description: "The target URL breaks the href rules." },
  broken_target: { label: "Target not reachable", description: "The target page did not answer 200." },
  block_not_found: { label: "Paragraph not found", description: "The source paragraph is not on the CMS page." },
  ambiguous_block: { label: "Paragraph ambiguous", description: "Several paragraphs match; none was chosen." },
  anchor_not_found: { label: "Anchor not found", description: "The anchor text is not at its place in the paragraph." },
  anchor_not_in_single_text_node: {
    label: "Anchor spans markup",
    description: "The anchor crosses formatting or another element in the page HTML.",
  },
  unmappable_span: { label: "Anchor cannot be mapped", description: "The anchor cannot be located exactly in the page HTML." },
  unsafe_placement: {
    label: "Unsafe placement",
    description: "The anchor is inside an excluded, hidden or already-linked element.",
  },
  overlapping_changes: { label: "Overlapping links", description: "Two plans of this request overlap." },
  already_applied: { label: "Already applied", description: "This link is already on the page." },
  conflicting_link: { label: "Conflicting link", description: "A different link with this id is already on the page." },
  self_check_failed: { label: "Self-check failed", description: "The change would alter more than the link itself." },
  deployment_in_progress: {
    label: "Deployment in progress",
    description: "Another deployment of this source page is running. Retry later; the plans are unchanged.",
  },
  density_exceeded: { label: "Too many links", description: "The page would exceed the link-density limit." },
  already_linked: { label: "Already linked", description: "The CMS page already links to this target." },
  cms_page_unmapped: { label: "No CMS page mapping", description: "This source page is not mapped to a CMS page." },
  rollback_conflict: {
    label: "Link changed since deployment",
    description: "Our link was itself edited on the page, so it was not removed.",
  },
  nothing_to_roll_back: { label: "Nothing to roll back", description: "Nothing of this deployment is on the page." },
};

const CHECK_NAME: Readonly<Record<VerificationCheckName, string>> = {
  link_present_once: "Link present exactly once",
  href_matches: "Link address matches the target",
  anchor_matches: "Anchor text matches",
  in_expected_block: "In the expected paragraph",
  not_hidden: "Not hidden",
  not_nested: "Not nested in another link",
  target_reachable: "Target page reachable",
};

const OUTCOME: Readonly<Record<VerificationOutcome, StatusPresentation>> = {
  passed: { label: "Passed", description: "The check passed.", tone: "positive", known: true },
  failed: {
    label: "Failed",
    description: "Deterministic failure: the page HTML or CMS state is wrong.",
    tone: "critical",
    known: true,
  },
  skipped: {
    label: "Skipped",
    description: "Not run in this configuration (e.g. no HTTP verification base URL).",
    tone: "neutral",
    known: true,
  },
  inconclusive: {
    label: "Inconclusive",
    description: "Transient: a network error, timeout, 5xx or publishing lag. It never fails a plan; re-verify later.",
    tone: "caution",
    known: true,
  },
};

const ROLLBACK_REASON: Readonly<Record<RollbackReason, Labelled>> = {
  verification_failed: { label: "Verification failed", description: "A check showed the link is wrong on the page." },
  editorial_reversal: { label: "Editorial reversal", description: "An editor decided the link should not be there." },
  wrong_destination: { label: "Wrong destination", description: "The link points at the wrong page." },
  broken_page: { label: "Broken page", description: "The page renders or behaves incorrectly." },
  other: { label: "Other (explain in the note)", description: "Requires a note." },
};

export const DEPLOYMENT_STATUSES = Object.keys(DEPLOYMENT_STATUS) as readonly DeploymentStatus[];
export const DEPLOYMENT_ISSUE_CODES = Object.keys(DEPLOYMENT_ISSUE) as readonly DeploymentIssueCode[];
export const VERIFICATION_CHECK_NAMES = Object.keys(CHECK_NAME) as readonly VerificationCheckName[];
export const VERIFICATION_OUTCOMES = Object.keys(OUTCOME) as readonly VerificationOutcome[];
export const ROLLBACK_REASONS = Object.keys(ROLLBACK_REASON) as readonly RollbackReason[];

export const describeDeploymentStatus = (value: string): StatusPresentation =>
  hasOwn(DEPLOYMENT_STATUS, value) ? DEPLOYMENT_STATUS[value] : unknownPresentation(value, "deployment status");
export const describeDeploymentIssue = (value: string): string =>
  hasOwn(DEPLOYMENT_ISSUE, value) ? DEPLOYMENT_ISSUE[value].label : unknownLabel(value);
export const describeDeploymentIssueDetail = (value: string): string | null =>
  hasOwn(DEPLOYMENT_ISSUE, value) ? DEPLOYMENT_ISSUE[value].description : null;
export const describeVerificationCheck = (value: string): string =>
  hasOwn(CHECK_NAME, value) ? CHECK_NAME[value] : unknownLabel(value);
export const describeVerificationOutcome = (value: string): StatusPresentation =>
  hasOwn(OUTCOME, value) ? OUTCOME[value] : unknownPresentation(value, "verification outcome");
export const describeRollbackReason = (value: string): string =>
  hasOwn(ROLLBACK_REASON, value) ? ROLLBACK_REASON[value].label : unknownLabel(value);
export const describeRollbackReasonDetail = (value: string): string | null =>
  hasOwn(ROLLBACK_REASON, value) ? ROLLBACK_REASON[value].description : null;

export function isRollbackReason(value: unknown): value is RollbackReason {
  return typeof value === "string" && hasOwn(ROLLBACK_REASON, value);
}

export function isDeploymentStatus(value: unknown): value is DeploymentStatus {
  return typeof value === "string" && hasOwn(DEPLOYMENT_STATUS, value);
}

/** A deployment's `error_code`: a deployment issue code, or another stable code (shown verbatim). */
export function describeDeploymentErrorCode(code: string): string {
  return hasOwn(DEPLOYMENT_ISSUE, code) ? DEPLOYMENT_ISSUE[code].label : unknownLabel(code);
}

/** One verification run's verdict (the run's own flags; never recomputed from the checks). */
export function describeVerificationRun(run: { readonly passed: boolean; readonly inconclusive?: boolean }): StatusPresentation {
  if (run.passed) return OUTCOME.passed;
  if (run.inconclusive === true) return OUTCOME.inconclusive;
  return OUTCOME.failed;
}

// ---------------------------------------------------------------------------
// What a deployment's status allows (the API re-checks)
// ---------------------------------------------------------------------------

/** Re-verify and roll back apply only to an applied deployment that is not mid-transition. */
const ACTIONABLE: ReadonlySet<string> = new Set<DeploymentStatus>(["applied", "verified", "verification_failed"]);
/** The CMS call is in flight or its outcome unknown; reconciliation settles it. */
const IN_PROGRESS: ReadonlySet<string> = new Set<DeploymentStatus>(["applying", "rolling_back"]);
/** Plan statuses whose link is (or may be) on the page, so a per-plan rollback can remove it. */
const ROLLBACKABLE_PLAN: ReadonlySet<string> = new Set(["deployed", "verified", "failed"]);

export interface DeploymentActions {
  readonly verify: boolean;
  readonly rollback: boolean;
}

export function availableDeploymentActions(status: string): DeploymentActions {
  const actionable = ACTIONABLE.has(status);
  return { verify: actionable, rollback: actionable };
}

export function isDeploymentInProgress(status: string): boolean {
  return IN_PROGRESS.has(status);
}

export function isRollbackablePlanStatus(status: string): boolean {
  return ROLLBACKABLE_PLAN.has(status);
}

// ---------------------------------------------------------------------------
// Preview: the anchor inside the block text (code-point offsets, as the API counts them)
// ---------------------------------------------------------------------------

/**
 * The block text split at `[anchor_start, anchor_end)`, or `null` when the offsets do not
 * select `anchor_text` verbatim (the UI then shows the text without a highlight).
 */
export function previewHighlight(preview: ChangePreview): HighlightParts | null {
  return splitHighlight(preview.block_text, preview.anchor_start, preview.anchor_end, preview.anchor_text);
}

/** Previews in the request's plan order (unknown plans last), for a stable rendering. */
export function previewsInOrder(previews: readonly ChangePreview[], planIds: readonly string[]): ChangePreview[] {
  const rank = new Map(planIds.map((id, index) => [id.toLowerCase(), index]));
  return [...previews].sort(
    (a, b) => (rank.get(a.plan_id.toLowerCase()) ?? planIds.length) - (rank.get(b.plan_id.toLowerCase()) ?? planIds.length),
  );
}

// ---------------------------------------------------------------------------
// Issues (dry-run results and refused mutations)
// ---------------------------------------------------------------------------

export interface IssueView {
  readonly code: string;
  readonly label: string;
  readonly description: string | null;
  /** The plan the issue concerns, or `null` for a request-level issue. */
  readonly planId: string | null;
}

const UUID_IN_TEXT = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

function issueView(code: string, planId: string | null): IssueView {
  return {
    code,
    label: describeDeploymentIssue(code),
    description: describeDeploymentIssueDetail(code),
    planId: planId === null ? null : planId.toLowerCase(),
  };
}

export function dryRunIssues(result: DryRunResult): IssueView[] {
  return result.issues.map((issue) => issueView(issue.code, issue.plan_id ?? null));
}

/**
 * The deployment issues of a refused mutation: `ErrorResponse.details` codes of a 409 or
 * 503 (a 422's details are field errors, not issues). The plan id, when present, is read
 * from the detail's `location` (a plan id, or a path ending in one).
 */
export function errorIssues(error: ApiError): IssueView[] {
  if (error.kind !== "api_error" || (error.status !== 409 && error.status !== 503)) return [];
  return (error.details ?? []).map((detail) => issueView(detail.code, detail.location?.match(UUID_IN_TEXT)?.[0] ?? null));
}

function hasIssue(error: ApiError, code: DeploymentIssueCode): boolean {
  return (error.details ?? []).some((detail) => detail.code === code);
}

/** An operator-facing explanation of a refused or failed deployment call. Never the raw body. */
export function deploymentErrorMessage(error: ApiError, operation: "dry_run" | "deploy" | "verify" | "rollback"): string {
  const unchanged =
    operation === "dry_run" || operation === "verify" ? "A dry run writes nothing." : "Nothing was changed on the site.";
  const nothing = operation === "verify" ? "Nothing was changed." : unchanged;
  if (error.kind === "api_error" || error.kind === "http_status") {
    switch (error.status) {
      case 401:
        return "Your session has ended. Sign in again; nothing was changed.";
      case 403:
        return "Your role does not permit deployments. Only an operator admin can deploy, verify or roll back. Nothing was changed.";
      case 404:
        return operation === "verify" || operation === "rollback"
          ? "This deployment no longer exists. Nothing was changed."
          : "A selected recommendation no longer exists. Nothing was changed.";
      case 409:
        if (error.code === "cms_version_conflict") {
          return (
            "The CMS page changed after it was checked, so nothing was applied. The selected plans have expired; the " +
            "recommendation engine regenerates them from the current content on its next run (make recommend)."
          );
        }
        if (hasIssue(error, "deployment_in_progress")) {
          return "Another deployment of this source page is running. Retry later; the plans are unchanged.";
        }
        if (hasIssue(error, "rollback_conflict")) {
          return (
            "A deployed link was itself changed on the page since deployment, so it was not removed. The deployment " +
            "and its plans are unchanged; check the page in the CMS."
          );
        }
        if (hasIssue(error, "nothing_to_roll_back")) {
          return "Nothing of this deployment is on the page, so there is nothing to roll back.";
        }
        if (errorIssues(error).length > 0) {
          return `The request was refused for the reasons listed. ${nothing}`;
        }
        return (
          "The request conflicts with the current state (it may have been reused with different content, or the " +
          `deployment changed since you loaded it). Reload and try again. ${nothing}`
        );
      case 422:
        return `The API rejected the input. ${nothing}${error.detail === undefined ? "" : ` The API said: ${error.detail}`}`;
      case 503:
        return `The CMS is unavailable right now. ${nothing} Try again later.`;
      default:
        break;
    }
  }
  if (error.kind === "timeout") {
    if (operation === "deploy" || operation === "rollback") {
      return (
        "The API did not respond in time. The change may still be in progress: check the deployment history before " +
        "retrying. Submitting this same form again is safe — it replays the same request instead of applying it twice."
      );
    }
    return "The API did not respond in time. Try again.";
  }
  if (error.kind === "network") return `The API could not be reached. ${nothing}`;
  if (error.kind === "invalid_argument") return "The deployment id is not valid; nothing was sent.";
  return `${error.message} Reload to check the current state before retrying.`;
}

// ---------------------------------------------------------------------------
// Validation (mirrors the contract's validators; the API re-validates)
// ---------------------------------------------------------------------------

/** NUL, other C0 controls (except tab, newline, carriage return), DEL and lone surrogates. */
const UNSTORABLE = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]|\p{Cs}/u;

export function validateNote(note: string, required: boolean): string | undefined {
  if (note.length > MAX_DEPLOY_NOTE_CHARS) return `The note may be at most ${MAX_DEPLOY_NOTE_CHARS} characters.`;
  if (UNSTORABLE.test(note)) return "The note contains an invalid character.";
  if (required && note.trim() === "") return "Explain the reason “Other” in the note.";
  return undefined;
}

/** Why a set of plan ids cannot be submitted, or `undefined`. */
export function validatePlanSelection(planIds: readonly string[]): string | undefined {
  if (planIds.length === 0) return "Select at least one approved recommendation.";
  if (planIds.length > MAX_PLANS_PER_DEPLOYMENT) {
    return `Select at most ${MAX_PLANS_PER_DEPLOYMENT} recommendations for one deployment.`;
  }
  if (!planIds.every(isUuid)) return "A selected recommendation id is not valid.";
  if (new Set(planIds.map((id) => id.toLowerCase())).size !== planIds.length) return "A recommendation was selected twice.";
  return undefined;
}

/** Why a selection spans several source pages (one deployment = one source page), or `undefined`. */
export function validateSameSource(sourcePageIds: readonly string[]): string | undefined {
  return new Set(sourcePageIds.map((id) => id.toLowerCase())).size > 1
    ? "One deployment covers one source page. Select recommendations of a single source page (filter the inbox by source page)."
    : undefined;
}

export interface RollbackFieldErrors {
  readonly reason?: string;
  readonly note?: string;
  readonly plans?: string;
}

export function validateRollbackInput(input: {
  readonly reason: string;
  readonly note: string;
  readonly planIds: readonly string[];
}): RollbackFieldErrors {
  const errors: { reason?: string; note?: string; plans?: string } = {};
  if (!isRollbackReason(input.reason)) errors.reason = "Choose a reason for the rollback.";
  const note = validateNote(input.note, input.reason === "other");
  if (note !== undefined) errors.note = note;
  if (input.planIds.length > 0) {
    const plans = validatePlanSelection(input.planIds);
    if (plans !== undefined) errors.plans = plans;
  }
  return errors;
}

export function hasErrors(errors: object): boolean {
  return Object.values(errors).some((message) => message !== undefined);
}

/**
 * The rollback's `plan_ids`: `null` (the whole deployment) when nothing or every
 * roll-back-able link is ticked, else the ticked subset (a per-plan REMOVE rollback).
 */
export function rollbackPlanIds(selected: readonly string[], eligible: readonly string[]): string[] | null {
  if (selected.length === 0) return null;
  const all = new Set(eligible.map((id) => id.toLowerCase()));
  const chosen = selected.map((id) => id.toLowerCase());
  return all.size > 0 && chosen.length === all.size && chosen.every((id) => all.has(id)) ? null : chosen;
}

/** The deployment's links a per-plan rollback may remove. */
export function rollbackablePlans(deployment: Pick<DeploymentDetail, "items">): DeploymentDetail["items"] {
  return deployment.items.filter((item) => isRollbackablePlanStatus(item.plan_status));
}

// ---------------------------------------------------------------------------
// Server-action states
// ---------------------------------------------------------------------------

export interface DeployFieldErrors {
  readonly plans?: string;
  readonly note?: string;
}

export type DryRunState =
  | { readonly status: "idle" }
  | { readonly status: "invalid"; readonly message: string; readonly fieldErrors: DeployFieldErrors }
  | { readonly status: "error"; readonly message: string; readonly code?: string; readonly issues: readonly IssueView[] }
  | {
      readonly status: "preview";
      /** New on every dry run: the confirm form is keyed by it, so each preview mints its own operation id. */
      readonly key: string;
      readonly planIds: readonly string[];
      readonly result: DryRunResult;
    };

export type DeployState =
  | { readonly status: "idle" }
  | { readonly status: "invalid"; readonly message: string; readonly fieldErrors: DeployFieldErrors }
  | { readonly status: "error"; readonly message: string; readonly code?: string; readonly issues: readonly IssueView[] };

export type DeploymentActionState =
  | { readonly status: "idle" }
  | { readonly status: "invalid"; readonly message: string; readonly fieldErrors: RollbackFieldErrors }
  | { readonly status: "error"; readonly message: string; readonly code?: string; readonly issues: readonly IssueView[] }
  | { readonly status: "success"; readonly message: string };

export const IDLE: { readonly status: "idle" } = { status: "idle" };
