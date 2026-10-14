"use client";

import { useActionState, useState } from "react";

import type { FeedbackReason, TargetOption } from "@/lib/api-client";
import { formatScore } from "@/lib/intelligence";
import { pageDisplayTitle } from "@/lib/inventory";
import {
  type AvailableActions,
  IDLE,
  type ReviewFormState,
  reasonRequired,
  reasonsFor,
} from "@/lib/review";

import { SpanPicker } from "./SpanPicker";
import {
  BUTTON_DANGER,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  FieldError,
  FormMessage,
  NoteField,
  ReasonFieldset,
  submitWith,
} from "./fields";

export type ReviewServerAction = (state: ReviewFormState, form: FormData) => Promise<ReviewFormState>;

export interface ReviewActions {
  readonly approve: ReviewServerAction;
  readonly reject: ReviewServerAction;
  readonly editAnchor: ReviewServerAction;
  readonly replaceTarget: ReviewServerAction;
}

export interface ReviewPanelProps {
  readonly planId: string;
  /** The status the reviewer sees; sent as `expected_status`. */
  readonly status: string;
  readonly statusLabel: string;
  readonly available: AvailableActions;
  readonly blockText: string;
  readonly contextHash: string;
  readonly anchorStart: number;
  readonly anchorEnd: number;
  /** Eligible replacement destinations, or `null` when they could not be loaded. */
  readonly targetOptions: readonly TargetOption[] | null;
  readonly actions: ReviewActions;
}

type Kind = "approve" | "reject" | "edit" | "replace";

const SECTION = "space-y-3 rounded border border-slate-300 p-4";

/**
 * Approve, reject, edit the anchor, or replace the destination of one plan.
 *
 * Rendered only for a session with the `review` permission, and only the
 * actions the plan's status allows. All four action states live here, so a
 * confirmation stays visible after the page refreshes with the new status
 * (when the form that produced it no longer applies).
 */
export function ReviewPanel(props: ReviewPanelProps) {
  const { actions, available } = props;
  const [approveState, approveAction, approvePending] = useActionState(actions.approve, IDLE);
  const [rejectState, rejectAction, rejectPending] = useActionState(actions.reject, IDLE);
  const [editState, editAction, editPending] = useActionState(actions.editAnchor, IDLE);
  const [replaceState, replaceAction, replacePending] = useActionState(actions.replaceTarget, IDLE);
  const [last, setLast] = useState<Kind | null>(null);
  const pending = approvePending || rejectPending || editPending || replacePending;
  const lastState =
    last === "approve" ? approveState : last === "reject" ? rejectState : last === "edit" ? editState : last === "replace" ? replaceState : IDLE;

  return (
    <div className="space-y-4">
      {lastState.status === "success" && <FormMessage tone="success" message={lastState.message} focusKey={lastState} />}
      {!available.approve && !available.reject && !available.edit ? (
        <p role="note" className="rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
          This recommendation is {props.statusLabel.toLowerCase()}; no review action applies to it.
        </p>
      ) : (
        <p className="text-sm text-slate-700">
          Every action is recorded with your name and reasons. Approving does not deploy anything: the site is unchanged
          until a later deployment step.
        </p>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        {available.approve && (
          <ReasonsForm
            kind="approve"
            title="Approve"
            submitLabel="Approve"
            buttonClass={BUTTON_PRIMARY}
            reasons={reasonsFor("approve")}
            required={reasonRequired("approve")}
            state={approveState}
            action={approveAction}
            pending={pending}
            onSubmit={() => setLast("approve")}
            planId={props.planId}
            status={props.status}
          />
        )}
        {available.reject && (
          <ReasonsForm
            kind="reject"
            title={props.status === "approved" ? "Reverse the approval (reject)" : "Reject"}
            submitLabel="Reject"
            buttonClass={BUTTON_DANGER}
            reasons={reasonsFor("reject")}
            required={reasonRequired("reject")}
            state={rejectState}
            action={rejectAction}
            pending={pending}
            onSubmit={() => setLast("reject")}
            planId={props.planId}
            status={props.status}
          />
        )}
      </div>
      {available.edit && (
        <EditAnchorForm {...props} state={editState} action={editAction} pending={pending} onSubmit={() => setLast("edit")} />
      )}
      {available.edit && (
        <ReplaceTargetForm
          {...props}
          state={replaceState}
          action={replaceAction}
          pending={pending}
          onSubmit={() => setLast("replace")}
        />
      )}
    </div>
  );
}

interface FormProps {
  readonly state: ReviewFormState;
  readonly action: (form: FormData) => void;
  readonly pending: boolean;
  readonly onSubmit: () => void;
  readonly planId: string;
  readonly status: string;
}

function Hidden({ planId, status }: { readonly planId: string; readonly status: string }) {
  return (
    <>
      <input type="hidden" name="plan_id" value={planId} />
      <input type="hidden" name="expected_status" value={status} />
    </>
  );
}

function Outcome({ state }: { readonly state: ReviewFormState }) {
  if (state.status === "invalid" || state.status === "error") {
    return <FormMessage tone="error" message={state.message} focusKey={state} />;
  }
  return null;
}

function fieldErrorsOf(state: ReviewFormState) {
  return state.status === "invalid" ? state.fieldErrors : {};
}

function ReasonsForm({
  kind,
  title,
  submitLabel,
  buttonClass,
  reasons,
  required,
  state,
  action,
  pending,
  onSubmit,
  planId,
  status,
}: FormProps & {
  readonly kind: "approve" | "reject";
  readonly title: string;
  readonly submitLabel: string;
  readonly buttonClass: string;
  readonly reasons: readonly FeedbackReason[];
  readonly required: boolean;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const errors = fieldErrorsOf(state);
  const headingId = `review-${kind}-heading`;
  return (
    <form onSubmit={submitWith(action, onSubmit)} aria-labelledby={headingId} className={SECTION}>
      <h3 id={headingId} className="text-lg font-semibold">
        {title}
      </h3>
      <Hidden planId={planId} status={status} />
      <Outcome state={state} />
      <ReasonFieldset
        idPrefix={`review-${kind}`}
        reasons={reasons}
        selected={selected}
        onChange={setSelected}
        required={required}
        error={errors.reasons}
      />
      <NoteField
        idPrefix={`review-${kind}`}
        value={note}
        onChange={setNote}
        required={selected.includes("other")}
        error={errors.note}
      />
      <button type="submit" disabled={pending} className={buttonClass}>
        {pending ? "Working…" : submitLabel}
      </button>
    </form>
  );
}

function ApproveNow({ idPrefix }: { readonly idPrefix: string }) {
  return (
    <p className="flex items-center gap-2 text-sm">
      <input id={`${idPrefix}-approve`} type="checkbox" name="approve" />
      <label htmlFor={`${idPrefix}-approve`}>Approve the edited recommendation now</label>
    </p>
  );
}

function EditAnchorForm(props: ReviewPanelProps & FormProps) {
  const [selected, setSelected] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const errors = fieldErrorsOf(props.state);
  return (
    <form onSubmit={submitWith(props.action, props.onSubmit)} aria-labelledby="review-edit-heading" className={SECTION}>
      <h3 id="review-edit-heading" className="text-lg font-semibold">
        Edit the anchor or placement
      </h3>
      <p className="text-sm text-slate-700">
        Choose a different span of the same paragraph. This creates a new recommendation that supersedes this one; the
        current one is kept in the history.
      </p>
      <Hidden planId={props.planId} status={props.status} />
      <input type="hidden" name="block_context_hash" value={props.contextHash} />
      <Outcome state={props.state} />
      <SpanPicker
        text={props.blockText}
        initialStart={props.anchorStart}
        initialEnd={props.anchorEnd}
        error={errors.span}
      />
      <ReasonFieldset
        idPrefix="review-edit"
        reasons={reasonsFor("edit_anchor")}
        selected={selected}
        onChange={setSelected}
        required
        error={errors.reasons}
      />
      <NoteField idPrefix="review-edit" value={note} onChange={setNote} required={selected.includes("other")} error={errors.note} />
      <ApproveNow idPrefix="review-edit" />
      <button type="submit" disabled={props.pending} className={BUTTON_SECONDARY}>
        {props.pending ? "Working…" : "Save edited anchor"}
      </button>
    </form>
  );
}

function ReplaceTargetForm(props: ReviewPanelProps & FormProps) {
  const [selected, setSelected] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [target, setTarget] = useState<string>("");
  const errors = fieldErrorsOf(props.state);
  const options = props.targetOptions;
  return (
    <form onSubmit={submitWith(props.action, props.onSubmit)} aria-labelledby="review-replace-heading" className={SECTION}>
      <h3 id="review-replace-heading" className="text-lg font-semibold">
        Replace the destination
      </h3>
      <p className="text-sm text-slate-700">
        Keep the anchor text and point it at a different page. Only eligible destinations are offered. This creates a
        new recommendation that supersedes this one.
      </p>
      {options === null ? (
        <p role="note" className="rounded border border-amber-700 bg-amber-50 p-2 text-sm text-amber-950">
          <span aria-hidden="true">! </span>
          Eligible destinations could not be loaded, so the destination cannot be replaced right now.
        </p>
      ) : options.length === 0 ? (
        <p role="note" className="text-sm text-slate-700">
          No other eligible destination exists for this anchor.
        </p>
      ) : (
        <>
          <Hidden planId={props.planId} status={props.status} />
          <Outcome state={props.state} />
          <fieldset className="space-y-1" aria-describedby={errors.target === undefined ? undefined : "review-replace-target-error"}>
            <legend className="font-medium text-slate-800">New destination</legend>
            <ul className="max-h-72 space-y-1 overflow-auto rounded border border-slate-300 p-2">
              {options.map((option) => {
                const id = `review-replace-target-${option.page_id}`;
                return (
                  <li key={option.page_id} className="flex items-start gap-2 text-sm">
                    <input
                      id={id}
                      type="radio"
                      name="target_page_id"
                      value={option.page_id}
                      checked={target === option.page_id}
                      onChange={() => setTarget(option.page_id)}
                      className="mt-1"
                    />
                    <label htmlFor={id} className="min-w-0">
                      <span className="font-medium">{pageDisplayTitle(option)}</span>
                      <span className="block break-all font-mono text-xs text-slate-600">{option.canonical_url}</span>
                      <span className="block text-xs text-slate-700">
                        Relevance {option.relevance === null ? "not computed" : formatScore(option.relevance)}
                        {option.page_type === null ? "" : ` · ${option.page_type}`}
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
            <FieldError id="review-replace-target-error" message={errors.target} />
          </fieldset>
          <ReasonFieldset
            idPrefix="review-replace"
            reasons={reasonsFor("replace_target")}
            selected={selected}
            onChange={setSelected}
            required
            error={errors.reasons}
          />
          <NoteField idPrefix="review-replace" value={note} onChange={setNote} required={selected.includes("other")} error={errors.note} />
          <ApproveNow idPrefix="review-replace" />
          <button type="submit" disabled={props.pending} className={BUTTON_SECONDARY}>
            {props.pending ? "Working…" : "Replace destination"}
          </button>
        </>
      )}
    </form>
  );
}
