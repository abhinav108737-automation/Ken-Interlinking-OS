"use server";
/**
 * Deployment mutations (ADR-0014 sections 1 and 7). Every one is a server action, so
 * Next.js applies its same-origin check and the session token stays on the server
 * (lib/api.ts forwards it; a 401 redirects to sign-in). The API requires `deploy`
 * (operator_admin) and derives the actor from the session; the actor is never sent.
 *
 * - A dry run writes nothing and never consumes an operation id, so each one mints a
 *   fresh id.
 * - Deploy and rollback send the form's `operation_id`, minted once per rendered form
 *   (a deploy form per dry-run preview), so a retry after a timeout replays instead of
 *   applying twice.
 * - Input is re-validated with lib/deployments.ts; the API validates again.
 */
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { createDeployment, dryRunDeployment, rollbackDeployment, verifyDeployment } from "@/lib/api";
import type { ApiError, RollbackReason } from "@/lib/api-client";
import {
  DEPLOYMENTS_PATH,
  type DeployState,
  type DeploymentActionState,
  type DryRunState,
  deploymentErrorMessage,
  deploymentHref,
  describeDeploymentStatus,
  errorIssues,
  hasErrors,
  rollbackPlanIds,
  validateNote,
  validatePlanSelection,
  validateRollbackInput,
} from "@/lib/deployments";
import { RECOMMENDATIONS_PATH, isUuid, planHref } from "@/lib/recommendations";
import { formText, formValues } from "@/lib/review";

/** The form's idempotency key, or a fresh one if absent or malformed. */
function operationIdFrom(form: FormData): string {
  const id = formText(form, "operation_id");
  return isUuid(id) ? id.toLowerCase() : crypto.randomUUID();
}

function planIdsFrom(form: FormData): string[] {
  return formValues(form, "plan_ids").map((id) => id.trim().toLowerCase());
}

function failure(error: ApiError, operation: Parameters<typeof deploymentErrorMessage>[1]) {
  return {
    status: "error" as const,
    message: deploymentErrorMessage(error, operation),
    issues: errorIssues(error),
    ...(error.code === undefined ? {} : { code: error.code }),
  };
}

function refreshPlans(planIds: readonly string[]): void {
  for (const planId of planIds) revalidatePath(planHref(planId));
  revalidatePath(RECOMMENDATIONS_PATH);
  revalidatePath(DEPLOYMENTS_PATH);
  revalidatePath("/");
}

export async function dryRunAction(_previous: DryRunState, form: FormData): Promise<DryRunState> {
  const planIds = planIdsFrom(form);
  const plans = validatePlanSelection(planIds);
  if (plans !== undefined) return { status: "invalid", message: "Correct the highlighted fields.", fieldErrors: { plans } };
  const result = await dryRunDeployment({ operation_id: crypto.randomUUID(), plan_ids: planIds, note: "" });
  if (!result.ok) return failure(result.error, "dry_run");
  return { status: "preview", key: crypto.randomUUID(), planIds, result: result.data };
}

export async function deployAction(_previous: DeployState, form: FormData): Promise<DeployState> {
  const planIds = planIdsFrom(form);
  const note = formText(form, "note");
  const plans = validatePlanSelection(planIds);
  const noteError = validateNote(note, false);
  if (plans !== undefined || noteError !== undefined) {
    return {
      status: "invalid",
      message: "Correct the highlighted fields.",
      fieldErrors: { ...(plans === undefined ? {} : { plans }), ...(noteError === undefined ? {} : { note: noteError }) },
    };
  }
  const result = await createDeployment({ operation_id: operationIdFrom(form), plan_ids: planIds, note });
  if (!result.ok) {
    // A version conflict expires the plans: their screens must show it.
    if (result.error.code === "cms_version_conflict") refreshPlans(planIds);
    return failure(result.error, "deploy");
  }
  refreshPlans(planIds);
  redirect(`${deploymentHref(result.data.id)}?deployed=1`);
}

function deploymentIdFrom(form: FormData): string | null {
  const id = formText(form, "deployment_id");
  return isUuid(id) ? id.toLowerCase() : null;
}

const OUT_OF_DATE = "This form is out of date. Reload the page and try again.";

export async function verifyAction(_previous: DeploymentActionState, form: FormData): Promise<DeploymentActionState> {
  const deploymentId = deploymentIdFrom(form);
  if (deploymentId === null) return { status: "invalid", message: OUT_OF_DATE, fieldErrors: {} };
  const result = await verifyDeployment(deploymentId);
  if (!result.ok) return failure(result.error, "verify");
  revalidatePath(deploymentHref(deploymentId));
  refreshPlans(result.data.plan_ids);
  return {
    status: "success",
    message: `Verification re-run. Deployment status: ${describeDeploymentStatus(result.data.status).label}.`,
  };
}

export async function rollbackAction(_previous: DeploymentActionState, form: FormData): Promise<DeploymentActionState> {
  const deploymentId = deploymentIdFrom(form);
  if (deploymentId === null) return { status: "invalid", message: OUT_OF_DATE, fieldErrors: {} };
  const reason = formText(form, "reason");
  const note = formText(form, "note");
  const selected = planIdsFrom(form);
  const fieldErrors = validateRollbackInput({ reason, note, planIds: selected });
  if (hasErrors(fieldErrors)) return { status: "invalid", message: "Correct the highlighted fields.", fieldErrors };
  const planIds = rollbackPlanIds(selected, formValues(form, "eligible_plan_ids"));
  const result = await rollbackDeployment(deploymentId, {
    operation_id: operationIdFrom(form),
    reason: reason as RollbackReason,
    note,
    plan_ids: planIds,
  });
  if (!result.ok) return failure(result.error, "rollback");
  revalidatePath(deploymentHref(deploymentId));
  refreshPlans(result.data.plan_ids);
  const replay = result.data.idempotent_replay ? " (This repeated an earlier identical request; nothing changed twice.)" : "";
  return {
    status: "success",
    message: `Rollback requested${planIds === null ? "" : ` for ${planIds.length} link${planIds.length === 1 ? "" : "s"}`}. Deployment status: ${describeDeploymentStatus(result.data.status).label}.${replay}`,
  };
}
