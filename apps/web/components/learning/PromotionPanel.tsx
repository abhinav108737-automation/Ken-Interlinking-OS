"use client";

import { useActionState, useState } from "react";

import type { PromotionPreview } from "@/lib/api-client";
import {
  ADVANCE_IDLE,
  type AdvanceState,
  MAX_NOTE_CHARS,
  PROPOSAL_REJECTION_REASONS,
  PROPOSAL_STATUSES,
  concludesAdvance,
  describeProposalRejectionReason,
  describeProposalStatus,
  describePromotionBlock,
  describePromotionRefusal,
  reasonRequiredFor,
} from "@/lib/learning";

import { StatusBadge } from "../StatusBadge";
import { BUTTON_PRIMARY, FieldError, FormMessage, submitWith } from "../review/fields";

export type AdvanceServerAction = (state: AdvanceState, form: FormData) => Promise<AdvanceState>;

export interface PromotionPanelProps {
  readonly proposalId: string;
  readonly currentStatus: string;
  /** The API's promotion preview: what promoting would do, and what refuses it. */
  readonly promotion: PromotionPreview | null;
  readonly action: AdvanceServerAction;
}

interface AdvanceRun {
  readonly state: AdvanceState;
  readonly round: number;
}

/**
 * Advance a proposal (ADR-0016; ADR-0018 §5 keeps policy and skill promotion a
 * governed human action). Offered only to a session holding `promote`; the API
 * enforces the permission regardless, and an agent can never reach this path.
 *
 * The promotion preview is shown FIRST, including every refusal that applies,
 * so an operator sees that demonstration mode blocks a promotion before they
 * try. The control is still rendered: hiding it would make a mechanical rule
 * look like a missing feature.
 *
 * The operation id is minted once per rendered form and re-minted only after a
 * recorded outcome, so a retry after a timeout replays rather than applying
 * twice.
 */
export function PromotionPanel({ proposalId, currentStatus, promotion, action }: PromotionPanelProps) {
  const [run, dispatch, pending] = useActionState(
    async (previous: AdvanceRun, form: FormData): Promise<AdvanceRun> => {
      const state = await action(previous.state, form);
      return { state, round: previous.round + (concludesAdvance(state) ? 1 : 0) };
    },
    { state: ADVANCE_IDLE, round: 0 } as AdvanceRun,
  );
  return (
    <section aria-labelledby="advance-heading" className="space-y-3 rounded border border-slate-400 bg-slate-50 p-4">
      <h2 id="advance-heading" className="text-xl font-semibold">
        Advance this proposal
      </h2>
      <p className="max-w-prose text-sm text-slate-800">
        A proposal moves through offline evaluation and then a shadow or canary run before it can be promoted. Only a
        person may promote one, and the promotion is recorded with your account and the evaluation it relies on.
      </p>
      {promotion !== null && (
        <div className="space-y-2 rounded border border-slate-300 bg-white p-3 text-sm">
          <h3 className="font-semibold">If this were promoted</h3>
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
            <dt className="font-medium text-slate-700">Next version</dt>
            <dd>
              <code className="text-xs">{promotion.next_version}</code>
            </dd>
            <dt className="font-medium text-slate-700">Rollback to</dt>
            <dd>
              {promotion.rollback_to_version === null ? (
                <span className="text-slate-600">no earlier version</span>
              ) : (
                <code className="text-xs">{promotion.rollback_to_version}</code>
              )}
            </dd>
            <dt className="font-medium text-slate-700">Files written</dt>
            <dd>
              {promotion.target_paths.length === 0 ? (
                <span className="text-slate-600">none</span>
              ) : (
                <ul className="list-none">
                  {promotion.target_paths.map((path) => (
                    <li key={path}>
                      <code className="break-all text-xs">{path}</code>
                    </li>
                  ))}
                </ul>
              )}
            </dd>
            <dt className="font-medium text-slate-700">Needs a restart</dt>
            <dd>{promotion.requires_operator_restart ? "yes — settings are frozen at start-up" : "no"}</dd>
          </dl>
          <p>
            <StatusBadge presentation={describePromotionBlock(promotion.blocked_by)} />
          </p>
          {promotion.blocked_by.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-5">
              {promotion.blocked_by.map((refusal) => (
                <li key={refusal}>
                  {describePromotionRefusal(refusal)}{" "}
                  <span className="font-mono text-xs text-slate-600">{refusal}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <AdvanceForm
        key={run.round}
        proposalId={proposalId}
        currentStatus={currentStatus}
        state={run.state}
        dispatch={dispatch}
        pending={pending}
      />
    </section>
  );
}

function AdvanceForm({
  proposalId,
  currentStatus,
  state,
  dispatch,
  pending,
}: {
  readonly proposalId: string;
  readonly currentStatus: string;
  readonly state: AdvanceState;
  readonly dispatch: (form: FormData) => void;
  readonly pending: boolean;
}) {
  const [operationId] = useState(() => crypto.randomUUID());
  const [toStatus, setToStatus] = useState("");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const errors = state.status === "invalid" ? state.fieldErrors : {};
  const needsReason = reasonRequiredFor(toStatus);
  const options = PROPOSAL_STATUSES.filter((value) => value !== currentStatus);
  return (
    <form onSubmit={submitWith(dispatch)} aria-labelledby="advance-heading" className="space-y-3">
      <input type="hidden" name="proposal_id" value={proposalId} />
      <input type="hidden" name="operation_id" value={operationId} />
      {(state.status === "invalid" || state.status === "error") && (
        <FormMessage tone="error" message={state.message} focusKey={state} />
      )}
      {state.status === "done" && <FormMessage tone="success" message={state.message} focusKey={state} />}
      <div className="space-y-1">
        <label htmlFor="advance-status" className="block font-medium text-slate-900">
          Move to (required)
        </label>
        <select
          id="advance-status"
          name="to_status"
          value={toStatus}
          onChange={(event) => setToStatus(event.target.value)}
          required
          aria-invalid={errors.to_status === undefined ? undefined : true}
          aria-describedby={
            errors.to_status === undefined ? "advance-status-hint" : "advance-status-hint advance-status-error"
          }
          className="rounded border border-slate-400 bg-white px-2 py-1 text-sm"
        >
          <option value="">Choose a status…</option>
          {options.map((value) => (
            <option key={value} value={value}>
              {describeProposalStatus(value).label}
            </option>
          ))}
        </select>
        <p id="advance-status-hint" className="max-w-prose text-xs text-slate-800">
          {toStatus === ""
            ? `Currently ${describeProposalStatus(currentStatus).label}. The API refuses a transition that is not allowed, or whose gate has not passed.`
            : describeProposalStatus(toStatus).description}
        </p>
        <FieldError id="advance-status-error" message={errors.to_status} />
      </div>
      <div className="space-y-1">
        <label htmlFor="advance-reason" className="block font-medium text-slate-900">
          Structured reason {needsReason ? "(required)" : "(optional)"}
        </label>
        <select
          id="advance-reason"
          name="reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          required={needsReason}
          aria-invalid={errors.reason === undefined ? undefined : true}
          aria-describedby={errors.reason === undefined ? undefined : "advance-reason-error"}
          className="rounded border border-slate-400 bg-white px-2 py-1 text-sm"
        >
          <option value="">No structured reason</option>
          {PROPOSAL_REJECTION_REASONS.map((value) => (
            <option key={value} value={value}>
              {describeProposalRejectionReason(value)}
            </option>
          ))}
        </select>
        <FieldError id="advance-reason-error" message={errors.reason} />
      </div>
      <div className="space-y-1">
        <label htmlFor="advance-evaluation" className="block font-medium text-slate-900">
          Evaluation run id (optional)
        </label>
        <input
          id="advance-evaluation"
          name="evaluation_run_id"
          type="text"
          maxLength={80}
          aria-invalid={errors.evaluation_run_id === undefined ? undefined : true}
          aria-describedby={
            errors.evaluation_run_id === undefined
              ? "advance-evaluation-hint"
              : "advance-evaluation-hint advance-evaluation-error"
          }
          className="w-full max-w-md rounded border border-slate-400 bg-white px-2 py-1 font-mono text-sm"
        />
        <p id="advance-evaluation-hint" className="text-xs text-slate-800">
          Required by the API for a promotion: the passing evaluation run this relies on.
        </p>
        <FieldError id="advance-evaluation-error" message={errors.evaluation_run_id} />
      </div>
      <div className="space-y-1">
        <label htmlFor="advance-note" className="block font-medium text-slate-900">
          Note (optional)
        </label>
        <textarea
          id="advance-note"
          name="note"
          rows={2}
          maxLength={MAX_NOTE_CHARS}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          aria-invalid={errors.note === undefined ? undefined : true}
          aria-describedby={errors.note === undefined ? "advance-note-hint" : "advance-note-hint advance-note-error"}
          className="w-full max-w-xl rounded border border-slate-400 bg-white px-2 py-1 text-sm"
        />
        <p id="advance-note-hint" className="text-xs text-slate-800">
          {note.length}/{MAX_NOTE_CHARS} characters. Stored as plain text in the transition record.
        </p>
        <FieldError id="advance-note-error" message={errors.note} />
      </div>
      <button type="submit" disabled={pending} className={BUTTON_PRIMARY}>
        {pending ? "Recording…" : "Record transition"}
      </button>
    </form>
  );
}
