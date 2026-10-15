"use client";

import { useActionState, useState } from "react";

import {
  type DeploymentActionState,
  type DeploymentActions,
  IDLE,
  ROLLBACK_REASONS,
  describeRollbackReason,
  describeRollbackReasonDetail,
} from "@/lib/deployments";

import { BUTTON_DANGER, BUTTON_SECONDARY, FieldError, FormMessage, NoteField, submitWith } from "../review/fields";
import { IssueList } from "./IssueList";

export type DeploymentServerAction = (state: DeploymentActionState, form: FormData) => Promise<DeploymentActionState>;

export interface DeploymentPanelActions {
  readonly verify: DeploymentServerAction;
  readonly rollback: DeploymentServerAction;
}

export interface DeploymentActionsPanelProps {
  readonly deploymentId: string;
  readonly available: DeploymentActions;
  /** The links a per-plan rollback may remove (plan id and anchor text). */
  readonly rollbackable: readonly { readonly plan_id: string; readonly anchor_text: string }[];
  readonly actions: DeploymentPanelActions;
  /** Shown when the deployment's status allows neither action (in progress or terminal). */
  readonly unavailableNote: string;
}

/**
 * The rollback outcome plus a counter of successful rollbacks. The rollback form is keyed by
 * the counter, so every successful (or recorded-refused) rollback mints a fresh operation id (a later per-plan or
 * whole rollback is a new operation, not a reuse), while a failed or timed-out submission
 * keeps the same form and id, so its retry replays instead of applying twice.
 */
interface RollbackRun {
  readonly state: DeploymentActionState;
  readonly round: number;
}

/**
 * Rollback refusals the API RECORDS against the operation id (ADR-0014 section 10, L1): a
 * replay of that id returns the same refusal, so a retry after the editor fixes the page
 * needs a fresh id. Any other failure (a 503, a timeout) keeps the id so its retry replays.
 */
const RECORDED_REFUSALS: ReadonlySet<string> = new Set(["rollback_conflict", "nothing_to_roll_back", "cms_version_conflict"]);

/** True when the submission reached a recorded outcome, so the next one is a new operation. */
export function concludesOperation(state: DeploymentActionState): boolean {
  if (state.status === "success") return true;
  if (state.status !== "error") return false;
  return state.code === "cms_version_conflict" || state.issues.some((issue) => RECORDED_REFUSALS.has(issue.code));
}

const SECTION = "space-y-3 rounded border border-slate-300 p-4";

/**
 * Re-verify and roll back one deployment. Rendered for a session with `deploy`; the forms
 * appear only while the deployment's status allows them (applied, verified, verification
 * failed). Both outcomes live here and the panel stays mounted across status changes, so a
 * confirmation, or a 503 whose outcome is unknown, stays visible after the page refreshes.
 */
export function DeploymentActionsPanel({
  deploymentId,
  available,
  rollbackable,
  actions,
  unavailableNote,
}: DeploymentActionsPanelProps) {
  const [verifyState, verify, verifyPending] = useActionState(actions.verify, IDLE as DeploymentActionState);
  const [rollbackRun, rollback, rollbackPending] = useActionState(
    async (previous: RollbackRun, form: FormData): Promise<RollbackRun> => {
      const state = await actions.rollback(previous.state, form);
      return { state, round: previous.round + (concludesOperation(state) ? 1 : 0) };
    },
    { state: IDLE, round: 0 } as RollbackRun,
  );
  const rollbackState = rollbackRun.state;
  const [last, setLast] = useState<"verify" | "rollback" | null>(null);
  const pending = verifyPending || rollbackPending;
  const lastState = last === "verify" ? verifyState : last === "rollback" ? rollbackState : IDLE;
  // An error whose form is no longer shown (the refreshed status allows no action) stays visible here.
  const formShown = last === "verify" ? available.verify : last === "rollback" ? available.rollback : true;

  return (
    <div className="space-y-4">
      {lastState.status === "success" && <FormMessage tone="success" message={lastState.message} focusKey={lastState} />}
      {!formShown && <Outcome state={lastState} />}
      {!available.verify && !available.rollback ? (
        <p role="note" className="max-w-prose rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
          {unavailableNote}
        </p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {available.verify && (
            <form onSubmit={submitWith(verify, () => setLast("verify"))} aria-labelledby="verify-heading" className={SECTION}>
              <h3 id="verify-heading" className="text-lg font-semibold">
                Re-verify
              </h3>
              <p className="text-sm text-slate-700">
                Reads the page back and re-runs every check. It records a new verification run and changes nothing on the
                site.
              </p>
              <input type="hidden" name="deployment_id" value={deploymentId} />
              <Outcome state={verifyState} />
              <button type="submit" disabled={pending} className={BUTTON_SECONDARY}>
                {verifyPending ? "Verifying…" : "Re-verify"}
              </button>
            </form>
          )}
          {available.rollback && (
            <RollbackForm
              key={rollbackRun.round}
              deploymentId={deploymentId}
              rollbackable={rollbackable}
              state={rollbackState}
              action={rollback}
              pending={pending}
              onSubmit={() => setLast("rollback")}
            />
          )}
        </div>
      )}
    </div>
  );
}

function Outcome({ state }: { readonly state: DeploymentActionState }) {
  if (state.status !== "invalid" && state.status !== "error") return null;
  return (
    <>
      <FormMessage tone="error" message={state.message} focusKey={state} />
      {state.status === "error" && <IssueList issues={state.issues} />}
    </>
  );
}

function RollbackForm({
  deploymentId,
  rollbackable,
  state,
  action,
  pending,
  onSubmit,
}: {
  readonly deploymentId: string;
  readonly rollbackable: DeploymentActionsPanelProps["rollbackable"];
  readonly state: DeploymentActionState;
  readonly action: (form: FormData) => void;
  readonly pending: boolean;
  readonly onSubmit: () => void;
}) {
  // One idempotency key per submission: the panel re-keys this form after each success, so
  // the next rollback is a new operation; a retry after a timeout or 503 replays.
  const [operationId] = useState(() => crypto.randomUUID());
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const errors = state.status === "invalid" ? state.fieldErrors : {};
  const reasonDetail = reason === "" ? null : describeRollbackReasonDetail(reason);
  return (
    <form onSubmit={submitWith(action, onSubmit)} aria-labelledby="rollback-heading" className={SECTION}>
      <h3 id="rollback-heading" className="text-lg font-semibold">
        Roll back
      </h3>
      <p className="text-sm text-slate-700">
        Removes this deployment&apos;s links from the page through the CMS adapter. Unrelated later edits to the page are
        kept. A link that was itself changed since deployment is not removed.
      </p>
      <input type="hidden" name="deployment_id" value={deploymentId} />
      <input type="hidden" name="operation_id" value={operationId} />
      {rollbackable.map((item) => (
        <input key={item.plan_id} type="hidden" name="eligible_plan_ids" value={item.plan_id} />
      ))}
      <Outcome state={state} />
      <div className="space-y-1">
        <label htmlFor="rollback-reason" className="block font-medium text-slate-800">
          Reason (required)
        </label>
        <select
          id="rollback-reason"
          name="reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          required
          aria-invalid={errors.reason === undefined ? undefined : true}
          aria-describedby={errors.reason === undefined ? "rollback-reason-hint" : "rollback-reason-hint rollback-reason-error"}
          className="rounded border border-slate-400 bg-white px-2 py-1 text-sm"
        >
          <option value="">Choose a reason…</option>
          {ROLLBACK_REASONS.map((value) => (
            <option key={value} value={value}>
              {describeRollbackReason(value)}
            </option>
          ))}
        </select>
        <p id="rollback-reason-hint" className="text-xs text-slate-700">
          {reasonDetail ?? "Recorded with your name in the deployment and audit log."}
        </p>
        <FieldError id="rollback-reason-error" message={errors.reason} />
      </div>
      <NoteField idPrefix="rollback" value={note} onChange={setNote} required={reason === "other"} error={errors.note} />
      {rollbackable.length > 1 && (
        <fieldset className="space-y-1" aria-describedby={errors.plans === undefined ? undefined : "rollback-plans-error"}>
          <legend className="font-medium text-slate-800">Links to roll back (optional)</legend>
          <p className="text-xs text-slate-700">Leave all unticked to roll back every link of this deployment.</p>
          <ul className="space-y-1">
            {rollbackable.map((item) => {
              const id = `rollback-plan-${item.plan_id}`;
              return (
                <li key={item.plan_id} className="flex items-start gap-2 text-sm">
                  <input
                    id={id}
                    type="checkbox"
                    name="plan_ids"
                    value={item.plan_id}
                    checked={selected.includes(item.plan_id)}
                    onChange={(event) =>
                      setSelected(
                        event.target.checked ? [...selected, item.plan_id] : selected.filter((value) => value !== item.plan_id),
                      )
                    }
                    className="mt-1"
                  />
                  <label htmlFor={id} className="break-words">
                    “{item.anchor_text}”
                  </label>
                </li>
              );
            })}
          </ul>
          <FieldError id="rollback-plans-error" message={errors.plans} />
        </fieldset>
      )}
      <button type="submit" disabled={pending} className={BUTTON_DANGER}>
        {pending ? "Working…" : "Roll back"}
      </button>
    </form>
  );
}
