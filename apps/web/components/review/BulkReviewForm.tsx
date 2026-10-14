"use client";

import Link from "next/link";
import { useActionState, useState } from "react";

import type { BulkAction } from "@/lib/api-client";
import { describeLinkPlanStatus, planHref } from "@/lib/recommendations";
import {
  BULK_REASON_ACTION,
  type BulkFormState,
  IDLE,
  MAX_BULK_PLANS,
  describeBulkItemError,
  reasonsFor,
} from "@/lib/review";

import { StatusBadge } from "../StatusBadge";
import { BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SECONDARY, FieldError, FormMessage, NoteField, ReasonFieldset, submitWith } from "./fields";

export const BULK_FORM_ID = "bulk-review-form";

export interface BulkReviewFormProps {
  readonly action: (state: BulkFormState, form: FormData) => Promise<BulkFormState>;
}

/**
 * Bulk approve or reject the selected recommendations (checkboxes in the table
 * belong to this form through `form="bulk-review-form"`). Only plans awaiting
 * approval are selectable; each is reviewed in its own transaction and the
 * per-plan outcome is listed. Edits are never bulk.
 */
export function BulkReviewForm({ action }: BulkReviewFormProps) {
  const [state, formAction, pending] = useActionState(action, IDLE as BulkFormState);
  const [bulkAction, setBulkAction] = useState<BulkAction>("approve");
  const [selected, setSelected] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const errors = state.status === "invalid" ? state.fieldErrors : {};

  function selectAll(checked: boolean) {
    document
      .querySelectorAll<HTMLInputElement>(`input[type="checkbox"][name="plan_ids"][form="${BULK_FORM_ID}"]`)
      .forEach((box) => {
        box.checked = checked;
      });
  }

  const reasons = reasonsFor(BULK_REASON_ACTION[bulkAction]);
  return (
    <form id={BULK_FORM_ID} onSubmit={submitWith(formAction)} aria-labelledby="bulk-heading" className="space-y-3 rounded border border-slate-300 bg-slate-50 p-4">
      <h3 id="bulk-heading" className="text-lg font-semibold">
        Bulk review
      </h3>
      <p className="text-sm text-slate-700">
        Select up to {MAX_BULK_PLANS} recommendations awaiting approval in the table below, then approve or reject them
        together. Each one is reviewed separately; any that changed meanwhile are left unchanged and listed.
      </p>
      {(state.status === "invalid" || state.status === "error") && (
        <FormMessage tone="error" message={state.message} focusKey={state} />
      )}
      <FieldError id="bulk-plans-error" message={errors.plans} />
      <p className="flex flex-wrap gap-2">
        <button type="button" className={BUTTON_SECONDARY} onClick={() => selectAll(true)}>
          Select all on this page
        </button>
        <button type="button" className={BUTTON_SECONDARY} onClick={() => selectAll(false)}>
          Clear selection
        </button>
      </p>
      <fieldset className="flex flex-wrap gap-4 text-sm">
        <legend className="font-medium text-slate-800">Action</legend>
        {(["approve", "reject"] as const).map((value) => (
          <span key={value} className="flex items-center gap-1">
            <input
              id={`bulk-action-${value}`}
              type="radio"
              name="bulk_action"
              value={value}
              checked={bulkAction === value}
              onChange={() => {
                setBulkAction(value);
                setSelected([]);
              }}
            />
            <label htmlFor={`bulk-action-${value}`}>{value === "approve" ? "Approve" : "Reject"}</label>
          </span>
        ))}
      </fieldset>
      <ReasonFieldset
        idPrefix="bulk"
        reasons={reasons}
        selected={selected}
        onChange={setSelected}
        required={bulkAction === "reject"}
        error={errors.reasons}
      />
      <NoteField idPrefix="bulk" value={note} onChange={setNote} required={selected.includes("other")} error={errors.note} />
      <button type="submit" disabled={pending} className={bulkAction === "approve" ? BUTTON_PRIMARY : BUTTON_DANGER}>
        {pending ? "Working…" : bulkAction === "approve" ? "Approve selected" : "Reject selected"}
      </button>
      {state.status === "done" && (
        <div className="space-y-2">
          <FormMessage tone="success" message={state.message} focusKey={state} />
          <BulkResults state={state} />
        </div>
      )}
    </form>
  );
}

/** Per-plan outcome of a bulk review. */
export function BulkResults({ state }: { readonly state: Extract<BulkFormState, { status: "done" }> }) {
  return (
    <ul aria-label="Bulk review results" className="space-y-1 text-sm">
      {state.result.items.map((item) => (
        <li key={item.plan_id} className="flex flex-wrap items-center gap-2">
          <span className={item.ok ? "font-medium text-emerald-900" : "font-medium text-red-900"}>
            <span aria-hidden="true">{item.ok ? "✓ " : "✕ "}</span>
            {item.ok ? "Done" : "Not changed"}
          </span>
          <Link href={planHref(item.plan_id)} className="break-all font-mono text-xs text-blue-800 underline underline-offset-2">
            {item.plan_id}
          </Link>
          {item.status !== null && <StatusBadge presentation={describeLinkPlanStatus(item.status)} />}
          {!item.ok && <span className="text-slate-800">{describeBulkItemError(item.error_code)}</span>}
        </li>
      ))}
    </ul>
  );
}
