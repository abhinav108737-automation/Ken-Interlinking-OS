"use server";
/**
 * Review mutations (ADR-0013 section 4). Every one is a server action, so
 * Next.js applies its same-origin check and the session token stays on the
 * server (lib/api.ts forwards it; a 401 redirects to sign-in).
 *
 * Each call:
 * - re-validates its input with the same rules the forms use (lib/review.ts);
 *   the API validates again and is the authority;
 * - sends the form's `operation_id` (idempotency key, minted once per rendered single-plan
 *   form; a fresh one per bulk submission, whose plan set changes between submissions);
 * - sends `expected_status` = the status the reviewer saw, so a plan that
 *   changed meanwhile is refused (409) instead of acted on.
 *
 * The actor is never sent: the API derives it from the session.
 */
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { approvePlan, bulkReviewPlans, editPlanAnchor, rejectPlan, replacePlanTarget } from "@/lib/api";
import type { LinkPlanStatus, ReviewResult } from "@/lib/api-client";
import { LINK_PLAN_STATUSES, RECOMMENDATIONS_PATH, describeLinkPlanStatus, isUuid, planHref } from "@/lib/recommendations";
import {
  type BulkFormState,
  type FeedbackReasonInput,
  type ReviewFormState,
  formInteger,
  formText,
  formValues,
  hasFieldErrors,
  isBulkAction,
  reviewErrorMessage,
  tallyBulk,
  validateBulkInput,
  validateReasonsAndNote,
} from "@/lib/review";

const CONTEXT_HASH = /^[0-9a-f]{64}$/;

/** The form's idempotency key (minted once per rendered form), or a fresh one if absent or
 *  malformed. A retry of the same submission therefore replays instead of conflicting. */
function operationIdFrom(form: FormData): string {
  const id = formText(form, "operation_id");
  return isUuid(id) ? id.toLowerCase() : crypto.randomUUID();
}

interface Common {
  readonly planId: string;
  readonly expectedStatus: LinkPlanStatus;
  readonly reasons: FeedbackReasonInput[];
  readonly note: string;
}

type Parsed<T> = { readonly ok: true; readonly value: T } | { readonly ok: false; readonly state: ReviewFormState };

function invalid(message: string, fieldErrors = {}): { readonly ok: false; readonly state: ReviewFormState } {
  return { ok: false, state: { status: "invalid", message, fieldErrors } };
}

function parseCommon(form: FormData, action: "approve" | "reject" | "edit_anchor" | "replace_target"): Parsed<Common> {
  const planId = formText(form, "plan_id");
  const expected = formText(form, "expected_status");
  if (!isUuid(planId) || !(LINK_PLAN_STATUSES as readonly string[]).includes(expected)) {
    return invalid("This form is out of date. Reload the page and try again.");
  }
  const reasons = formValues(form, "reasons") as FeedbackReasonInput[];
  const note = formText(form, "note");
  const fieldErrors = validateReasonsAndNote(action, { reasons, note });
  if (hasFieldErrors(fieldErrors)) return invalid("Correct the highlighted fields.", fieldErrors);
  return { ok: true, value: { planId: planId.toLowerCase(), expectedStatus: expected as LinkPlanStatus, reasons, note } };
}

function succeeded(message: string, result: ReviewResult): ReviewFormState {
  const replay = result.idempotent_replay ? " (This repeated an earlier identical request; nothing changed twice.)" : "";
  return { status: "success", message: `${message}${replay}`, result };
}

function refresh(planId: string): void {
  revalidatePath(planHref(planId));
  revalidatePath(RECOMMENDATIONS_PATH);
  revalidatePath("/");
}

export async function approveAction(_previous: ReviewFormState, form: FormData): Promise<ReviewFormState> {
  const parsed = parseCommon(form, "approve");
  if (!parsed.ok) return parsed.state;
  const { planId, expectedStatus, reasons, note } = parsed.value;
  const result = await approvePlan(planId, {
    operation_id: operationIdFrom(form),
    expected_status: expectedStatus,
    reasons,
    note,
  });
  if (!result.ok) return { status: "error", message: reviewErrorMessage(result.error), ...codeOf(result.error) };
  refresh(planId);
  return succeeded(
    `Approved. Status: ${describeLinkPlanStatus(result.data.status).label}. Nothing is deployed yet; the site is unchanged.`,
    result.data,
  );
}

export async function rejectAction(_previous: ReviewFormState, form: FormData): Promise<ReviewFormState> {
  const parsed = parseCommon(form, "reject");
  if (!parsed.ok) return parsed.state;
  const { planId, expectedStatus, reasons, note } = parsed.value;
  const result = await rejectPlan(planId, {
    operation_id: operationIdFrom(form),
    expected_status: expectedStatus,
    reasons,
    note,
  });
  if (!result.ok) return { status: "error", message: reviewErrorMessage(result.error), ...codeOf(result.error) };
  refresh(planId);
  return succeeded(`Rejected. Status: ${describeLinkPlanStatus(result.data.status).label}.`, result.data);
}

function approveNow(form: FormData): boolean {
  return formText(form, "approve") === "on";
}

export async function editAnchorAction(_previous: ReviewFormState, form: FormData): Promise<ReviewFormState> {
  const parsed = parseCommon(form, "edit_anchor");
  if (!parsed.ok) return parsed.state;
  const { planId, expectedStatus, reasons, note } = parsed.value;
  const start = formInteger(form, "start");
  const end = formInteger(form, "end");
  const hash = formText(form, "block_context_hash");
  if (start === null || end === null || end <= start) {
    return { status: "invalid", message: "Correct the highlighted fields.", fieldErrors: { span: "Choose a span: the end must come after the start." } };
  }
  if (!CONTEXT_HASH.test(hash)) return invalid("This form is out of date. Reload the page and try again.").state;
  const result = await editPlanAnchor(planId, {
    operation_id: operationIdFrom(form),
    expected_status: expectedStatus,
    reasons,
    note,
    block_context_hash: hash,
    start,
    end,
    approve: approveNow(form),
  });
  if (!result.ok) return { status: "error", message: reviewErrorMessage(result.error), ...codeOf(result.error) };
  refresh(planId);
  if (result.data.new_plan_id !== null) redirect(`${planHref(result.data.new_plan_id)}?edited=1`);
  return succeeded("Anchor edited.", result.data);
}

export async function replaceTargetAction(_previous: ReviewFormState, form: FormData): Promise<ReviewFormState> {
  const parsed = parseCommon(form, "replace_target");
  if (!parsed.ok) return parsed.state;
  const { planId, expectedStatus, reasons, note } = parsed.value;
  const target = formText(form, "target_page_id");
  if (!isUuid(target)) {
    return { status: "invalid", message: "Correct the highlighted fields.", fieldErrors: { target: "Choose a new destination page." } };
  }
  const result = await replacePlanTarget(planId, {
    operation_id: operationIdFrom(form),
    expected_status: expectedStatus,
    reasons,
    note,
    target_page_id: target.toLowerCase(),
    approve: approveNow(form),
  });
  if (!result.ok) return { status: "error", message: reviewErrorMessage(result.error), ...codeOf(result.error) };
  refresh(planId);
  if (result.data.new_plan_id !== null) redirect(`${planHref(result.data.new_plan_id)}?edited=1`);
  return succeeded("Destination replaced.", result.data);
}

export async function bulkReviewAction(_previous: BulkFormState, form: FormData): Promise<BulkFormState> {
  const action = formText(form, "bulk_action");
  if (!isBulkAction(action)) {
    return { status: "invalid", message: "Choose approve or reject.", fieldErrors: {} };
  }
  const planIds = formValues(form, "plan_ids").map((id) => id.toLowerCase());
  const reasons = formValues(form, "reasons") as FeedbackReasonInput[];
  const note = formText(form, "note");
  const fieldErrors = validateBulkInput(action, planIds, { reasons, note });
  if (hasFieldErrors(fieldErrors)) return { status: "invalid", message: "Correct the highlighted fields.", fieldErrors };
  const result = await bulkReviewPlans({ operation_id: crypto.randomUUID(), action, plan_ids: planIds, reasons, note });
  if (!result.ok) return { status: "error", message: reviewErrorMessage(result.error), ...codeOf(result.error) };
  revalidatePath(RECOMMENDATIONS_PATH);
  revalidatePath("/");
  const { succeeded: done, failed } = tallyBulk(result.data);
  const verb = action === "approve" ? "approved" : "rejected";
  return {
    status: "done",
    message: `${done} of ${result.data.items.length} ${verb}${failed > 0 ? `; ${failed} not changed (see below)` : ""}.`,
    result: result.data,
  };
}

function codeOf(error: { readonly code?: string }): { code?: string } {
  return error.code === undefined ? {} : { code: error.code };
}
