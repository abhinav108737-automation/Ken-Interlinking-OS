/**
 * Human review of recommendations: presentation and form logic (ADR-0013 section 4).
 *
 * Portable and pure (no React, no fetch). The rule tables mirror the FROZEN
 * contract `ken_contracts.review` (REASONS_BY_ACTION, REVIEWABLE_STATUSES, …)
 * so the forms offer only what the API accepts; the API re-validates every
 * request and is the authority. Labels follow the lib/status.ts convention:
 * `Record`s keyed by generated enums, "Unknown (…)" for unexpected values.
 */
import type {
  ApiError,
  BulkAction,
  BulkReviewResult,
  ErrorCode,
  FeedbackReason,
  LinkPlanStatus,
  ReviewAction,
  ReviewResult,
} from "./api-client";
import { isUuid } from "./recommendations";

function hasOwn<T extends object>(record: T, key: string): key is Extract<keyof T, string> {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function unknownLabel(value: string): string {
  return `Unknown (${value.trim() === "" ? "empty" : value})`;
}

// ---------------------------------------------------------------------------
// Contract limits and rule tables (ken_contracts.review)
// ---------------------------------------------------------------------------

export const MAX_NOTE_CHARS = 500;
export const MAX_REASONS = 5;
export const MAX_BULK_PLANS = 50;

const REVIEWABLE: readonly LinkPlanStatus[] = ["proposed", "critiqued", "policy_checked", "awaiting_approval"];
/** A human may approve a plan in one of these. */
export const REVIEWABLE_STATUSES: ReadonlySet<string> = new Set(REVIEWABLE);
/** `approved -> rejected` reverses an approval before deployment. */
export const REJECTABLE_STATUSES: ReadonlySet<string> = new Set<LinkPlanStatus>([...REVIEWABLE, "approved"]);
/** An approved plan may still be edited (superseded) before deployment. */
export const EDITABLE_STATUSES: ReadonlySet<string> = new Set<LinkPlanStatus>([...REVIEWABLE, "approved"]);
/** Bulk review applies only to plans awaiting approval. */
export const BULK_STATUS: LinkPlanStatus = "awaiting_approval";

export interface AvailableActions {
  readonly approve: boolean;
  readonly reject: boolean;
  readonly edit: boolean;
}

/** Which review actions the plan's status allows (the API re-checks, including staleness). */
export function availableActions(status: string): AvailableActions {
  return {
    approve: REVIEWABLE_STATUSES.has(status),
    reject: REJECTABLE_STATUSES.has(status),
    edit: EDITABLE_STATUSES.has(status),
  };
}

/** Reasons in display order, per action — exactly `REASONS_BY_ACTION` of the contract. */
export const REASONS_BY_ACTION: Readonly<Record<ReviewAction, readonly FeedbackReason[]>> = {
  approve: ["good_as_is", "other"],
  reject: [
    "irrelevant_destination",
    "better_destination_exists",
    "misleading_anchor",
    "unnatural_anchor",
    "anchor_too_generic",
    "wrong_placement",
    "over_linking",
    "duplicate_of_existing_link",
    "outdated_or_changed_content",
    "sensitive_or_protected",
    "reader_journey_mismatch",
    "other",
  ],
  edit_anchor: [
    "misleading_anchor",
    "unnatural_anchor",
    "anchor_too_generic",
    "wrong_placement",
    "over_linking",
    "other",
  ],
  replace_target: [
    "irrelevant_destination",
    "better_destination_exists",
    "reader_journey_mismatch",
    "duplicate_of_existing_link",
    "sensitive_or_protected",
    "other",
  ],
};

/** Actions that need at least one reason (reject and both edits). */
const REASON_REQUIRED: Readonly<Record<ReviewAction, boolean>> = {
  approve: false,
  reject: true,
  edit_anchor: true,
  replace_target: true,
};

export const reasonsFor = (action: ReviewAction): readonly FeedbackReason[] => REASONS_BY_ACTION[action];
export const reasonRequired = (action: ReviewAction): boolean => REASON_REQUIRED[action];

/** The single-plan action whose reason set a bulk action uses. */
export const BULK_REASON_ACTION: Readonly<Record<BulkAction, ReviewAction>> = { approve: "approve", reject: "reject" };

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

const FEEDBACK_REASON: Readonly<Record<FeedbackReason, { readonly label: string; readonly description: string }>> = {
  good_as_is: { label: "Good as is", description: "The link is right as proposed." },
  irrelevant_destination: { label: "Irrelevant destination", description: "The target page does not fit this context." },
  better_destination_exists: {
    label: "A better destination exists",
    description: "Another page would serve the reader better.",
  },
  misleading_anchor: { label: "Misleading anchor", description: "The anchor promises something the target does not deliver." },
  unnatural_anchor: { label: "Unnatural anchor", description: "The anchor text reads awkwardly as a link." },
  anchor_too_generic: {
    label: "Anchor too generic",
    description: "The anchor is accurate but too general to say what the target covers. A more specific phrase would be better.",
  },
  wrong_placement: { label: "Wrong placement", description: "The link belongs elsewhere in the page." },
  over_linking: { label: "Over-linking", description: "The page or paragraph already has enough links." },
  duplicate_of_existing_link: {
    label: "Duplicates an existing link",
    description: "The page already links to this destination.",
  },
  outdated_or_changed_content: {
    label: "Outdated or changed content",
    description: "The source or target content has changed.",
  },
  sensitive_or_protected: { label: "Sensitive or protected", description: "The page should not be linked this way." },
  reader_journey_mismatch: {
    label: "Reader journey mismatch",
    description: "The destination does not fit where the reader is going.",
  },
  other: { label: "Other (explain in the note)", description: "Requires a note." },
};

const REVIEW_ACTION_LABEL: Readonly<Record<ReviewAction, string>> = {
  approve: "Approved",
  reject: "Rejected",
  edit_anchor: "Edited anchor",
  replace_target: "Replaced destination",
};

const BULK_ACTION_LABEL: Readonly<Record<BulkAction, string>> = {
  approve: "Approve selected",
  reject: "Reject selected",
};

export const FEEDBACK_REASONS = Object.keys(FEEDBACK_REASON) as readonly FeedbackReason[];
export const REVIEW_ACTIONS = Object.keys(REVIEW_ACTION_LABEL) as readonly ReviewAction[];
export const BULK_ACTIONS = Object.keys(BULK_ACTION_LABEL) as readonly BulkAction[];

export const describeFeedbackReason = (value: string): string =>
  hasOwn(FEEDBACK_REASON, value) ? FEEDBACK_REASON[value].label : unknownLabel(value);
export const describeFeedbackReasonDetail = (value: string): string | null =>
  hasOwn(FEEDBACK_REASON, value) ? FEEDBACK_REASON[value].description : null;
export const describeReviewAction = (value: string): string =>
  hasOwn(REVIEW_ACTION_LABEL, value) ? REVIEW_ACTION_LABEL[value] : unknownLabel(value);
export const describeBulkAction = (value: string): string =>
  hasOwn(BULK_ACTION_LABEL, value) ? BULK_ACTION_LABEL[value] : unknownLabel(value);

export function isFeedbackReason(value: unknown): value is FeedbackReason {
  return typeof value === "string" && hasOwn(FEEDBACK_REASON, value);
}

export function isReviewAction(value: unknown): value is ReviewAction {
  return typeof value === "string" && hasOwn(REVIEW_ACTION_LABEL, value);
}

export function isBulkAction(value: unknown): value is BulkAction {
  return typeof value === "string" && hasOwn(BULK_ACTION_LABEL, value);
}

// ---------------------------------------------------------------------------
// Validation (mirrors the contract's model validators; the API re-validates)
// ---------------------------------------------------------------------------

/** A reason as submitted (validated against the action's set before it is sent). */
export type FeedbackReasonInput = FeedbackReason;

export interface ReasonsAndNote {
  readonly reasons: readonly string[];
  readonly note: string;
}

export interface FieldErrors {
  readonly reasons?: string;
  readonly note?: string;
  readonly span?: string;
  readonly target?: string;
  readonly plans?: string;
}

/** Field errors for `action`'s reasons and note; empty when acceptable. */
export function validateReasonsAndNote(action: ReviewAction, input: ReasonsAndNote): FieldErrors {
  const errors: { reasons?: string; note?: string } = {};
  const allowed = new Set<string>(REASONS_BY_ACTION[action]);
  if (new Set(input.reasons).size !== input.reasons.length) {
    errors.reasons = "Each reason may be chosen once.";
  } else if (input.reasons.some((reason) => !allowed.has(reason))) {
    errors.reasons = "One of the chosen reasons does not apply to this action.";
  } else if (input.reasons.length > MAX_REASONS) {
    errors.reasons = `Choose at most ${MAX_REASONS} reasons.`;
  } else if (REASON_REQUIRED[action] && input.reasons.length === 0) {
    errors.reasons = "Choose at least one reason.";
  }
  if (input.note.length > MAX_NOTE_CHARS) {
    errors.note = `The note may be at most ${MAX_NOTE_CHARS} characters.`;
  } else if (input.note.includes("\u0000")) {
    errors.note = "The note contains an invalid character.";
  } else if (input.reasons.includes("other") && input.note.trim() === "") {
    errors.note = "Explain the reason “Other” in the note.";
  }
  return errors;
}

export function hasFieldErrors(errors: FieldErrors): boolean {
  return Object.values(errors).some((message) => message !== undefined);
}

/** Field errors for a bulk request; empty when acceptable. */
export function validateBulkInput(action: BulkAction, planIds: readonly string[], input: ReasonsAndNote): FieldErrors {
  const errors: FieldErrors = validateReasonsAndNote(BULK_REASON_ACTION[action], input);
  let plans: string | undefined;
  if (planIds.length === 0) plans = "Select at least one recommendation.";
  else if (planIds.length > MAX_BULK_PLANS) plans = `Select at most ${MAX_BULK_PLANS} recommendations at once.`;
  else if (new Set(planIds).size !== planIds.length) plans = "A recommendation was selected twice.";
  else if (!planIds.every(isUuid)) plans = "A selected recommendation id is not valid.";
  return plans === undefined ? errors : { ...errors, plans };
}

// ---------------------------------------------------------------------------
// Span picker (edit anchor). Offsets are Unicode CODE POINTS of the block text,
// as the API counts them (Python string indices); JavaScript string indices are
// UTF-16 units and diverge after an astral character (emoji, some CJK).
// ---------------------------------------------------------------------------

export interface WordToken {
  /** Index among word tokens. */
  readonly index: number;
  readonly text: string;
  /** Code-point offsets `[start, end)` in the block text. */
  readonly start: number;
  readonly end: number;
}

export interface TextPiece {
  readonly text: string;
  /** The word token, or `null` for the separator text between words. */
  readonly word: WordToken | null;
}

/**
 * A "word": letters, numbers and combining marks, joined by single internal
 * hyphens or apostrophes (`lithium-ion`, `O’Brien`). Deterministic in every
 * runtime, unlike locale-dependent segmenters.
 */
const WORD = /[\p{L}\p{N}][\p{L}\p{N}\p{M}]*(?:['’\-‐][\p{L}\p{N}][\p{L}\p{N}\p{M}]*)*/gu;

/** The block text as alternating separator and word pieces (concatenation = the text). */
export function tokenizeWords(text: string): TextPiece[] {
  const pieces: TextPiece[] = [];
  let lastUnit = 0;
  let codePoint = 0;
  let index = 0;
  for (const match of text.matchAll(WORD)) {
    const unitStart = match.index;
    const gap = text.slice(lastUnit, unitStart);
    if (gap !== "") pieces.push({ text: gap, word: null });
    codePoint += Array.from(gap).length;
    const length = Array.from(match[0]).length;
    pieces.push({ text: match[0], word: { index, text: match[0], start: codePoint, end: codePoint + length } });
    codePoint += length;
    index += 1;
    lastUnit = unitStart + match[0].length;
  }
  const tail = text.slice(lastUnit);
  if (tail !== "") pieces.push({ text: tail, word: null });
  return pieces;
}

export function wordsOf(pieces: readonly TextPiece[]): WordToken[] {
  return pieces.flatMap((piece) => (piece.word === null ? [] : [piece.word]));
}

/** The span from word `a` through word `b` (either order), in code points. */
export function spanOfWords(words: readonly WordToken[], a: number, b: number): { start: number; end: number } | null {
  const first = words[Math.min(a, b)];
  const last = words[Math.max(a, b)];
  if (first === undefined || last === undefined) return null;
  return { start: first.start, end: last.end };
}

/** Words fully inside `[start, end)`, as an index range, or `null` when the span is not on word boundaries. */
export function wordRangeOfSpan(
  words: readonly WordToken[],
  start: number,
  end: number,
): { first: number; last: number } | null {
  const first = words.findIndex((word) => word.start === start);
  const last = words.findIndex((word) => word.end === end);
  return first === -1 || last === -1 || last < first ? null : { first, last };
}

/** `text[start:end]` in code points, or `null` for an out-of-range or empty span. */
export function sliceCodePoints(text: string, start: number, end: number): string | null {
  const points = Array.from(text);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end <= start || end > points.length) {
    return null;
  }
  return points.slice(start, end).join("");
}

/** Why a span cannot be submitted, or `null`. The API applies the full placement and anchor rules. */
export function validateSpan(text: string, start: number, end: number): string | null {
  const slice = sliceCodePoints(text, start, end);
  if (slice === null) return "Choose a span inside the block text: the end must come after the start.";
  if (slice.trim() === "") return "The anchor cannot be only whitespace.";
  if (slice !== slice.trim()) return "The anchor cannot start or end with whitespace.";
  return null;
}

// ---------------------------------------------------------------------------
// Results and errors of review mutations (server actions return these)
// ---------------------------------------------------------------------------

export type ReviewFormState =
  | { readonly status: "idle" }
  | { readonly status: "invalid"; readonly message: string; readonly fieldErrors: FieldErrors }
  | { readonly status: "error"; readonly message: string; readonly code?: string }
  | { readonly status: "success"; readonly message: string; readonly result: ReviewResult };

export type BulkFormState =
  | { readonly status: "idle" }
  | { readonly status: "invalid"; readonly message: string; readonly fieldErrors: FieldErrors }
  | { readonly status: "error"; readonly message: string; readonly code?: string }
  | { readonly status: "done"; readonly message: string; readonly result: BulkReviewResult };

export const IDLE: { readonly status: "idle" } = { status: "idle" };

/** A reviewer-facing explanation of a refused or failed review call. Never shows the raw body. */
export function reviewErrorMessage(error: ApiError): string {
  if (error.kind === "api_error" || error.kind === "http_status") {
    const detail = error.detail === undefined ? "" : ` The API said: ${error.detail}`;
    switch (error.status) {
      case 401:
        return "Your session has ended. Sign in again; nothing was changed.";
      case 403:
        return "Your role does not permit reviewing recommendations. Nothing was changed.";
      case 404:
        return "This recommendation (or the chosen page) no longer exists. Nothing was changed.";
      case 409:
        if (error.code === "stale_page_version") {
          return (
            "The source page has changed since this recommendation was made, so it can no longer be approved or " +
            "edited. The engine regenerates it on its next run. Nothing was changed."
          );
        }
        return (
          "This recommendation changed since you loaded it (another reviewer may have acted on it), or the request " +
          `conflicts with its current state. Reload to see its current status. Nothing was changed.${detail}`
        );
      case 422:
        return `The API rejected the input. Nothing was changed.${detail}`;
      default:
        break;
    }
  }
  if (error.kind === "timeout") {
    return "The API did not respond in time. The action may or may not have been applied: reload to check before retrying.";
  }
  if (error.kind === "network") {
    return "The API could not be reached. Nothing was changed.";
  }
  if (error.kind === "invalid_argument") {
    return "The recommendation id is not valid; nothing was sent.";
  }
  return `${error.message} Reload to check the recommendation's status before retrying.`;
}

const BULK_ITEM_ERROR: Partial<Readonly<Record<ErrorCode, string>>> = {
  conflict: "Not awaiting approval any more, or changed since it was loaded",
  stale_page_version: "Source page changed (stale); the engine regenerates it",
  not_found: "No longer exists",
  validation_failed: "Rejected by validation",
  permission_denied: "Not permitted",
  protected_page: "A page is protected",
};

/** Label of a failed bulk item's `error_code` (a free string in the contract; an ErrorCode value). */
export function describeBulkItemError(code: string | null): string {
  if (code === null || code.trim() === "") return "Failed (no reason given)";
  return hasOwn(BULK_ITEM_ERROR, code) ? (BULK_ITEM_ERROR[code] ?? unknownLabel(code)) : unknownLabel(code);
}

export interface BulkTally {
  readonly succeeded: number;
  readonly failed: number;
}

export function tallyBulk(result: BulkReviewResult): BulkTally {
  const succeeded = result.items.filter((item) => item.ok).length;
  return { succeeded, failed: result.items.length - succeeded };
}

/** Values of a repeated form field (checkbox group), as strings. */
export function formValues(form: FormData, name: string): string[] {
  return form.getAll(name).filter((value): value is string => typeof value === "string");
}

/** A single text field, or `""`. */
export function formText(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value : "";
}

/** A non-negative integer field, or `null`. */
export function formInteger(form: FormData, name: string): number | null {
  const raw = formText(form, name).trim();
  if (!/^\d{1,9}$/.test(raw)) return null;
  return Number.parseInt(raw, 10);
}
