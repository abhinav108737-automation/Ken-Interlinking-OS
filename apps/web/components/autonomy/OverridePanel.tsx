"use client";

import { useActionState, useState } from "react";

import {
  MAX_REASON_CHARS,
  OVERRIDE_ACTIONS,
  OVERRIDE_IDLE,
  type OverrideState,
  concludesOverride,
  describeBlockCode,
  describeOverrideAction,
  describeOverrideActionDetail,
} from "@/lib/autonomy";

import { BUTTON_DANGER, FieldError, FormMessage, submitWith } from "../review/fields";

export type OverrideServerAction = (state: OverrideState, form: FormData) => Promise<OverrideState>;

export interface OverridePanelProps {
  readonly planId: string;
  /** The plan status this form was built from; a mismatch is refused server-side. */
  readonly expectedStatus: string;
  /** The blocks firing right now, so the form can say which are not overridable. */
  readonly blocks: readonly string[];
  /** True for a guarded deferral (a BLOCK with no codes): overridable, unlike a hard block. */
  readonly deferral?: boolean;
  readonly action: OverrideServerAction;
  /** True when the operator has already acted on this plan. */
  readonly alreadyOverridden: boolean;
}

/**
 * The operator override (ADR-0018 §5): publish or block one specific plan with
 * a recorded reason. Offered only to a session holding `deploy`; the API
 * enforces the permission regardless.
 *
 * IDEMPOTENCY (the Phase 6 lesson). The operation id is minted ONCE per
 * rendered form and re-minted only after a RECORDED outcome, because:
 *
 *  - a retry after a timeout or a 503 must REPLAY the same operation rather
 *    than apply a second time, so it keeps the id; and
 *  - a new override after a recorded success or a recorded refusal is a NEW
 *    operation, so the form is re-keyed and mints a fresh id.
 *
 * A failure whose outcome is unknown therefore never re-keys, and the message
 * it shows never claims nothing changed.
 */
interface OverrideRun {
  readonly state: OverrideState;
  readonly round: number;
}

export function OverridePanel({
  planId,
  expectedStatus,
  blocks,
  deferral = false,
  action,
  alreadyOverridden,
}: OverridePanelProps) {
  const [run, dispatch, pending] = useActionState(
    async (previous: OverrideRun, form: FormData): Promise<OverrideRun> => {
      const state = await action(previous.state, form);
      return { state, round: previous.round + (concludesOverride(state) ? 1 : 0) };
    },
    { state: OVERRIDE_IDLE, round: 0 } as OverrideRun,
  );
  return (
    <section aria-labelledby="override-heading" className="space-y-3 rounded border-2 border-red-700 bg-red-50 p-4">
      <h2 id="override-heading" className="text-xl font-semibold text-red-950">
        Operator override — an exception, not the normal path
      </h2>
      <p className="max-w-prose text-sm text-red-950">
        Normal link publishing is policy-governed and needs no approval. This control exists only for exceptions: it
        publishes or blocks <em>this one plan</em> and records your account and your reason in the audit log.
      </p>
      {alreadyOverridden && (
        <p role="note" className="max-w-prose rounded border border-slate-400 bg-white p-2 text-sm text-slate-900">
          An operator has already acted on this plan. Acting again is a new, separately recorded operation.
        </p>
      )}
      {deferral && blocks.length === 0 && (
        <p className="max-w-prose rounded border border-amber-700 bg-white p-2 text-sm text-amber-950">
          <span className="font-semibold">This is a deferral, not a hard block.</span> No deterministic rule forbids
          publishing it, so a “publish” override can succeed. It is recorded with your account and your reason.
        </p>
      )}
      {blocks.length > 0 && (
        <p className="max-w-prose rounded border border-red-800 bg-white p-2 text-sm text-red-950">
          <span className="font-semibold">A deterministic block is never overridable into a publish.</span> These are
          still firing: {blocks.map(describeBlockCode).join(", ")}. A “publish” override will be refused and the refusal
          recorded.
        </p>
      )}
      <OverrideForm
        key={run.round}
        planId={planId}
        expectedStatus={expectedStatus}
        state={run.state}
        dispatch={dispatch}
        pending={pending}
      />
    </section>
  );
}

function Outcome({ state }: { readonly state: OverrideState }) {
  if (state.status === "invalid") {
    return <FormMessage tone="error" message={state.message} focusKey={state} />;
  }
  if (state.status === "error") {
    return <FormMessage tone="error" message={state.message} focusKey={state} />;
  }
  if (state.status === "done") {
    // A refusal is reported as an error even though the HTTP call succeeded:
    // `applied: false` means nothing changed, and that must not read as success.
    return <FormMessage tone={state.applied ? "success" : "error"} message={state.message} focusKey={state} />;
  }
  return null;
}

function OverrideForm({
  planId,
  expectedStatus,
  state,
  dispatch,
  pending,
}: {
  readonly planId: string;
  readonly expectedStatus: string;
  readonly state: OverrideState;
  readonly dispatch: (form: FormData) => void;
  readonly pending: boolean;
}) {
  // One idempotency key per submission. The panel re-keys this form after a
  // recorded outcome, so a retry after a timeout or 503 replays instead of
  // applying twice.
  const [operationId] = useState(() => crypto.randomUUID());
  const [overrideAction, setOverrideAction] = useState("");
  const [reason, setReason] = useState("");
  const errors = state.status === "invalid" ? state.fieldErrors : {};
  const detail = overrideAction === "" ? null : describeOverrideActionDetail(overrideAction);
  return (
    <form onSubmit={submitWith(dispatch)} aria-labelledby="override-heading" className="space-y-3">
      <input type="hidden" name="plan_id" value={planId} />
      <input type="hidden" name="operation_id" value={operationId} />
      <input type="hidden" name="expected_status" value={expectedStatus} />
      <Outcome state={state} />
      <div className="space-y-1">
        <label htmlFor="override-action" className="block font-medium text-slate-900">
          Action (required)
        </label>
        <select
          id="override-action"
          name="action"
          value={overrideAction}
          onChange={(event) => setOverrideAction(event.target.value)}
          required
          aria-invalid={errors.action === undefined ? undefined : true}
          aria-describedby={
            errors.action === undefined ? "override-action-hint" : "override-action-hint override-action-error"
          }
          className="rounded border border-slate-400 bg-white px-2 py-1 text-sm"
        >
          <option value="">Choose an action…</option>
          {OVERRIDE_ACTIONS.map((value) => (
            <option key={value} value={value}>
              {describeOverrideAction(value)}
            </option>
          ))}
        </select>
        <p id="override-action-hint" className="max-w-prose text-xs text-slate-800">
          {detail ?? "Recorded with your name, the plan's current status and your reason."}
        </p>
        <FieldError id="override-action-error" message={errors.action} />
      </div>
      <div className="space-y-1">
        <label htmlFor="override-reason" className="block font-medium text-slate-900">
          Reason (required)
        </label>
        <textarea
          id="override-reason"
          name="reason"
          rows={3}
          maxLength={MAX_REASON_CHARS}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          required
          aria-invalid={errors.reason === undefined ? undefined : true}
          aria-describedby={
            errors.reason === undefined ? "override-reason-hint" : "override-reason-hint override-reason-error"
          }
          className="w-full max-w-xl rounded border border-slate-400 bg-white px-2 py-1 text-sm"
        />
        <p id="override-reason-hint" className="text-xs text-slate-800">
          {reason.length}/{MAX_REASON_CHARS} characters. Stored as plain text in the audit log and shown to other
          operators.
        </p>
        <FieldError id="override-reason-error" message={errors.reason} />
      </div>
      <FieldError id="override-status-error" message={errors.expected_status} />
      <button type="submit" disabled={pending} className={BUTTON_DANGER}>
        {pending ? "Recording…" : "Record override"}
      </button>
    </form>
  );
}
