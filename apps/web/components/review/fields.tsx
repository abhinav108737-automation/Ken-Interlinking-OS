"use client";

import { type FormEvent, startTransition, useEffect, useRef } from "react";

import type { FeedbackReason } from "@/lib/api-client";
import { MAX_NOTE_CHARS, describeFeedbackReason, describeFeedbackReasonDetail } from "@/lib/review";

export const BUTTON_PRIMARY =
  "rounded border border-blue-800 bg-blue-800 px-3 py-1 font-medium text-white hover:bg-blue-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 disabled:opacity-60";
export const BUTTON_DANGER =
  "rounded border border-red-800 bg-red-800 px-3 py-1 font-medium text-white hover:bg-red-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 disabled:opacity-60";
export const BUTTON_SECONDARY =
  "rounded border border-slate-500 bg-white px-3 py-1 text-slate-900 hover:bg-slate-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 disabled:opacity-60";

function FieldError({ id, message }: { readonly id: string; readonly message: string | undefined }) {
  if (message === undefined) return null;
  return (
    <p id={id} className="text-sm font-medium text-red-900">
      <span aria-hidden="true">✕ </span>
      {message}
    </p>
  );
}

/** Checkbox group of structured reasons (controlled, so a refused submit keeps them). */
export function ReasonFieldset({
  idPrefix,
  reasons,
  selected,
  onChange,
  required,
  error,
  legend,
}: {
  readonly idPrefix: string;
  readonly reasons: readonly FeedbackReason[];
  readonly selected: readonly string[];
  readonly onChange: (next: string[]) => void;
  readonly required: boolean;
  readonly error?: string | undefined;
  readonly legend?: string;
}) {
  const errorId = `${idPrefix}-reasons-error`;
  return (
    <fieldset className="space-y-1" aria-describedby={error === undefined ? undefined : errorId}>
      <legend className="font-medium text-slate-800">
        {legend ?? (required ? "Reasons (at least one)" : "Reasons (optional)")}
      </legend>
      <ul className="grid gap-1 sm:grid-cols-2">
        {reasons.map((reason) => {
          const id = `${idPrefix}-reason-${reason}`;
          const checked = selected.includes(reason);
          return (
            <li key={reason} className="flex items-start gap-2 text-sm">
              <input
                id={id}
                type="checkbox"
                name="reasons"
                value={reason}
                checked={checked}
                onChange={(event) =>
                  onChange(
                    event.target.checked ? [...selected, reason] : selected.filter((value) => value !== reason),
                  )
                }
                className="mt-1"
              />
              <label htmlFor={id} title={describeFeedbackReasonDetail(reason) ?? undefined}>
                {describeFeedbackReason(reason)}
              </label>
            </li>
          );
        })}
      </ul>
      <FieldError id={errorId} message={error} />
    </fieldset>
  );
}

/** Free-text note (plain text; ≤ 500 characters; required for the reason "Other"). */
export function NoteField({
  idPrefix,
  value,
  onChange,
  required,
  error,
}: {
  readonly idPrefix: string;
  readonly value: string;
  readonly onChange: (next: string) => void;
  readonly required: boolean;
  readonly error?: string | undefined;
}) {
  const id = `${idPrefix}-note`;
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block font-medium text-slate-800">
        Note {required ? "(required for “Other”)" : "(optional)"}
      </label>
      <textarea
        id={id}
        name="note"
        rows={2}
        maxLength={MAX_NOTE_CHARS}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-describedby={error === undefined ? hintId : `${hintId} ${errorId}`}
        aria-invalid={error === undefined ? undefined : true}
        required={required}
        className="w-full max-w-xl rounded border border-slate-400 bg-white px-2 py-1 text-sm"
      />
      <p id={hintId} className="text-xs text-slate-700">
        {value.length}/{MAX_NOTE_CHARS} characters. Shown to other reviewers as plain text.
      </p>
      <FieldError id={errorId} message={error} />
    </div>
  );
}

export { FieldError };

/**
 * The outcome of a submit. Errors are alerts and take focus, so keyboard and
 * screen-reader users land on the explanation; successes are polite status.
 */
export function FormMessage({
  tone,
  message,
  focusKey,
}: {
  readonly tone: "error" | "success";
  readonly message: string;
  /** Changes on every new outcome, so focus moves again for a repeated error. */
  readonly focusKey: unknown;
}) {
  const ref = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (tone === "error") ref.current?.focus();
  }, [tone, focusKey]);
  return tone === "error" ? (
    <p ref={ref} tabIndex={-1} role="alert" className="rounded border border-red-700 bg-red-50 p-2 text-sm text-red-950">
      <span aria-hidden="true">✕ </span>
      {message}
    </p>
  ) : (
    <p ref={ref} tabIndex={-1} role="status" className="rounded border border-emerald-700 bg-emerald-50 p-2 text-sm text-emerald-950">
      <span aria-hidden="true">✓ </span>
      {message}
    </p>
  );
}

/**
 * Submit handler that dispatches a `useActionState` action WITHOUT React's
 * automatic form reset (which applies to `<form action>`). The review forms
 * use controlled fields; a reset would clear the DOM but not the state, so a
 * refused submit would silently drop the reviewer's reasons.
 */
export function submitWith(dispatch: (form: FormData) => void, before?: () => void) {
  return (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    before?.();
    const data = new FormData(event.currentTarget);
    startTransition(() => dispatch(data));
  };
}
