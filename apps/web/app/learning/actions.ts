"use server";
/**
 * Advancing a learning proposal (ADR-0016; ADR-0018 §5). A server action, so
 * Next.js applies its same-origin check and the session token stays on the
 * server. The API requires `promote` and derives the actor from the session.
 *
 * `demonstration_mode` refuses a promotion server-side. This action surfaces
 * that refusal as the error it is; it never reports a refusal as a success, and
 * it never claims nothing changed when the outcome is unknown.
 */
import { revalidatePath } from "next/cache";

import { advanceLearningProposal } from "@/lib/api";
import type { ApiError, ProposalRejectionReason, ProposalStatus } from "@/lib/api-client";
import {
  LEARNING_PATH,
  PROPOSALS_PATH,
  SKILLS_PATH,
  type AdvanceState,
  advanceErrorMessage,
  advanceOutcomeUnknown,
  describeAdvanceOutcome,
  hasAdvanceErrors,
  proposalHref,
  validateAdvanceInput,
} from "@/lib/learning";
import { isUuid } from "@/lib/recommendations";
import { formText } from "@/lib/review";

const OUT_OF_DATE = "This form is out of date. Reload the page and try again.";

function operationIdFrom(form: FormData): string {
  const id = formText(form, "operation_id");
  return isUuid(id) ? id.toLowerCase() : crypto.randomUUID();
}

function refresh(proposalId: string): void {
  revalidatePath(proposalHref(proposalId));
  revalidatePath(PROPOSALS_PATH);
  revalidatePath(SKILLS_PATH);
  revalidatePath(LEARNING_PATH);
}

function failure(error: ApiError): AdvanceState {
  return {
    status: "error",
    message: advanceErrorMessage(error),
    outcomeUnknown: advanceOutcomeUnknown(error),
    ...(error.code === undefined ? {} : { code: error.code }),
  };
}

export async function advanceAction(_previous: AdvanceState, form: FormData): Promise<AdvanceState> {
  const proposalId = formText(form, "proposal_id");
  if (!isUuid(proposalId)) {
    return { status: "invalid", message: OUT_OF_DATE, fieldErrors: {} };
  }
  const toStatus = formText(form, "to_status");
  const reason = formText(form, "reason");
  const note = formText(form, "note");
  const evaluationRunId = formText(form, "evaluation_run_id");
  const fieldErrors = validateAdvanceInput({ toStatus, reason, note, evaluationRunId });
  if (hasAdvanceErrors(fieldErrors)) {
    return { status: "invalid", message: "Correct the highlighted fields.", fieldErrors };
  }
  const result = await advanceLearningProposal(proposalId.toLowerCase(), {
    operation_id: operationIdFrom(form),
    to_status: toStatus as ProposalStatus,
    reason: reason === "" ? null : (reason as ProposalRejectionReason),
    note: note === "" ? null : note,
    evaluation_run_id: evaluationRunId.trim() === "" ? null : evaluationRunId.trim().toLowerCase(),
  });
  if (!result.ok) {
    if (advanceOutcomeUnknown(result.error) || result.error.status === 409) refresh(proposalId.toLowerCase());
    return failure(result.error);
  }
  refresh(proposalId.toLowerCase());
  return describeAdvanceOutcome(result.data.transition.to_status, result.data.replayed);
}
