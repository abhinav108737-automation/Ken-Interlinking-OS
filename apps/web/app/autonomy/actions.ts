"use server";
/**
 * The operator override (ADR-0018 §5). A server action, so Next.js applies its
 * same-origin check and the session token stays on the server (lib/api.ts
 * forwards it; a 401 redirects to sign-in). The API requires `deploy` and
 * derives the actor from the session; the actor is never sent from here.
 *
 * Three properties this file exists to hold:
 *
 * 1. The form's `operation_id` is sent as given, so a retry of the SAME
 *    submission after a timeout or a 503 replays instead of applying twice.
 *    The panel mints it once per rendered form and re-mints it only after a
 *    recorded outcome.
 * 2. A 503 or a timeout means the outcome is UNKNOWN. It is never reported as
 *    "nothing changed": the message says so and the affected screens are
 *    revalidated so the operator sees the real state.
 * 3. A 200 with `applied: false` is a RECORDED REFUSAL, not a success. A
 *    deterministic block is never overridable into a publish, and
 *    `describeOverrideOutcome` says so.
 */
import { revalidatePath } from "next/cache";

import { overrideAutonomyDecision } from "@/lib/api";
import type { ApiError, LinkPlanStatus, OverrideAction } from "@/lib/api-client";
import {
  ATTEMPTS_PATH,
  AUTONOMY_PATH,
  BLOCKED_PATH,
  DECISIONS_PATH,
  type OverrideState,
  blockedPlanHref,
  describeOverrideOutcome,
  hasOverrideErrors,
  overrideErrorMessage,
  overrideOutcomeUnknown,
  validateOverrideInput,
} from "@/lib/autonomy";
import { RECOMMENDATIONS_PATH, isUuid, planHref } from "@/lib/recommendations";
import { formText } from "@/lib/review";

/** The form's idempotency key, or a fresh one if absent or malformed. */
function operationIdFrom(form: FormData): string {
  const id = formText(form, "operation_id");
  return isUuid(id) ? id.toLowerCase() : crypto.randomUUID();
}

const OUT_OF_DATE = "This form is out of date. Reload the page and try again.";

/** Every screen whose content an override can change. */
function refresh(planId: string): void {
  revalidatePath(blockedPlanHref(planId));
  revalidatePath(planHref(planId));
  revalidatePath(BLOCKED_PATH);
  revalidatePath(DECISIONS_PATH);
  revalidatePath(ATTEMPTS_PATH);
  revalidatePath(AUTONOMY_PATH);
  revalidatePath(RECOMMENDATIONS_PATH);
  revalidatePath("/");
}

function failure(error: ApiError): OverrideState {
  const unknown = overrideOutcomeUnknown(error);
  return {
    status: "error",
    message: overrideErrorMessage(error),
    outcomeUnknown: unknown,
    ...(error.code === undefined ? {} : { code: error.code }),
  };
}

export async function overrideAction(_previous: OverrideState, form: FormData): Promise<OverrideState> {
  const planId = formText(form, "plan_id");
  if (!isUuid(planId)) {
    return { status: "invalid", message: OUT_OF_DATE, fieldErrors: {} };
  }
  const action = formText(form, "action");
  const reason = formText(form, "reason");
  const expectedStatus = formText(form, "expected_status");
  const fieldErrors = validateOverrideInput({ action, reason, expectedStatus });
  if (hasOverrideErrors(fieldErrors)) {
    return { status: "invalid", message: "Correct the highlighted fields.", fieldErrors };
  }
  const result = await overrideAutonomyDecision(planId.toLowerCase(), {
    operation_id: operationIdFrom(form),
    action: action as OverrideAction,
    reason: reason.trim(),
    expected_status: expectedStatus as LinkPlanStatus,
  });
  if (!result.ok) {
    // A 409 expired the form's view of the plan, and a 503 may have left the
    // override half-applied: either way the screens must show the real state.
    if (overrideOutcomeUnknown(result.error) || result.error.status === 409) refresh(planId.toLowerCase());
    return failure(result.error);
  }
  refresh(planId.toLowerCase());
  return describeOverrideOutcome(result.data);
}
