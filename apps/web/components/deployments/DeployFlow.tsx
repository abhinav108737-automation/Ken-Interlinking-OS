"use client";

import { type FormEvent, startTransition, useActionState, useState } from "react";

import {
  type DeployState,
  type DryRunState,
  IDLE,
  MAX_PLANS_PER_DEPLOYMENT,
  dryRunIssues,
  previewsInOrder,
  validateSameSource,
} from "@/lib/deployments";

import { BUTTON_DANGER, BUTTON_SECONDARY, FieldError, FormMessage, NoteField, submitWith } from "../review/fields";
import { ChangePreviewView } from "./ChangePreviewView";
import { IssueList } from "./IssueList";

export type DryRunServerAction = (state: DryRunState, form: FormData) => Promise<DryRunState>;
export type DeployServerAction = (state: DeployState, form: FormData) => Promise<DeployState>;

export interface DeployFlowActions {
  readonly dryRun: DryRunServerAction;
  readonly deploy: DeployServerAction;
}

/** Id of the inbox's dry-run form; the table's checkboxes join it through `form=`. */
export const DEPLOY_SELECT_FORM_ID = "deploy-dry-run-form";

export interface DeployFlowProps {
  readonly actions: DeployFlowActions;
  /** One plan (its detail screen). Omit to deploy the inbox's ticked checkboxes. */
  readonly planId?: string;
  /** Anchor text by (lower-case) plan id, for headings and issue links. */
  readonly planLabels?: Readonly<Record<string, string>>;
}

const SECTION = "space-y-3 rounded border border-slate-300 p-4";

/**
 * Dry run → review the preview → deploy. Rendered only for a session with `deploy`.
 * The dry run writes nothing; "Deploy" sends exactly the previewed plans with an
 * operation id minted for that preview, then opens the new deployment.
 */
export function DeployFlow({ actions, planId, planLabels = {} }: DeployFlowProps) {
  const [state, dryRun, pending] = useActionState(actions.dryRun, IDLE as DryRunState);
  const [localError, setLocalError] = useState<{ readonly message: string } | null>(null);
  const select = planId === undefined;

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    if (select) {
      const ticked = Array.from(
        document.querySelectorAll<HTMLInputElement>(`input[type="checkbox"][name="plan_ids"][form="${DEPLOY_SELECT_FORM_ID}"]`),
      ).filter((box) => box.checked);
      const mixed = validateSameSource(ticked.map((box) => box.dataset["sourcePageId"] ?? ""));
      if (mixed !== undefined) {
        setLocalError({ message: mixed });
        return;
      }
    }
    setLocalError(null);
    const data = new FormData(form);
    startTransition(() => dryRun(data));
  }

  const fieldErrors = state.status === "invalid" ? state.fieldErrors : {};
  return (
    <div className="space-y-4">
      <form
        {...(select ? { id: DEPLOY_SELECT_FORM_ID } : {})}
        onSubmit={onSubmit}
        aria-labelledby="deploy-heading"
        className={SECTION}
      >
        <h3 id="deploy-heading" className="text-lg font-semibold">
          Deploy
        </h3>
        <p className="text-sm text-slate-700">
          {select
            ? `Tick up to ${MAX_PLANS_PER_DEPLOYMENT} approved recommendations of ONE source page in the table below, then run a dry run. `
            : "Run a dry run first. "}
          A dry run checks the plans against the CMS page as it is now and shows the exact change. It writes nothing.
        </p>
        {planId !== undefined && <input type="hidden" name="plan_ids" value={planId} />}
        {localError !== null && <FormMessage tone="error" message={localError.message} focusKey={localError} />}
        {(state.status === "invalid" || state.status === "error") && (
          <FormMessage tone="error" message={state.message} focusKey={state} />
        )}
        {state.status === "error" && <IssueList issues={state.issues} planLabels={planLabels} />}
        <FieldError id="deploy-plans-error" message={fieldErrors.plans} />
        <button type="submit" disabled={pending} className={BUTTON_SECONDARY}>
          {pending ? "Checking…" : "Dry run"}
        </button>
      </form>
      {state.status === "preview" && (
        <DryRunPreview key={state.key} state={state} deploy={actions.deploy} planLabels={planLabels} />
      )}
    </div>
  );
}

function DryRunPreview({
  state,
  deploy,
  planLabels,
}: {
  readonly state: Extract<DryRunState, { status: "preview" }>;
  readonly deploy: DeployServerAction;
  readonly planLabels: Readonly<Record<string, string>>;
}) {
  const { result, planIds } = state;
  const issues = dryRunIssues(result);
  const previews = previewsInOrder(result.changes, planIds);
  const count = planIds.length;
  const links = `${count} link${count === 1 ? "" : "s"}`;
  return (
    <section aria-labelledby="dry-run-heading" className={SECTION}>
      <h3 id="dry-run-heading" className="text-lg font-semibold">
        Dry-run result
      </h3>
      {result.valid ? (
        <p role="status" className="rounded border border-emerald-700 bg-emerald-50 p-2 text-sm text-emerald-950">
          <span aria-hidden="true">✓ </span>
          Valid: {links} can be applied
          {result.cms_page_id === null || result.cms_page_id === undefined ? "" : ` to CMS page ${result.cms_page_id}`}
          {result.cms_version === null || result.cms_version === undefined ? "" : ` (version ${result.cms_version})`}.
          Nothing has been written yet.
        </p>
      ) : (
        <p role="alert" className="rounded border border-red-700 bg-red-50 p-2 text-sm text-red-950">
          <span aria-hidden="true">✕ </span>
          Not deployable: fix or drop the recommendations listed below. Nothing was written.
        </p>
      )}
      <IssueList issues={issues} planLabels={planLabels} />
      {previews.length > 0 && (
        <div className="space-y-3">
          <h4 className="font-medium">Exact change per recommendation</h4>
          {previews.map((preview) => {
            const label = planLabels[preview.plan_id.toLowerCase()];
            return <ChangePreviewView key={preview.plan_id} preview={preview} {...(label === undefined ? {} : { label })} />;
          })}
        </div>
      )}
      {result.valid && <DeployConfirm planIds={planIds} deploy={deploy} links={links} planLabels={planLabels} />}
    </section>
  );
}

function DeployConfirm({
  planIds,
  deploy,
  links,
  planLabels,
}: {
  readonly planIds: readonly string[];
  readonly deploy: DeployServerAction;
  readonly links: string;
  readonly planLabels: Readonly<Record<string, string>>;
}) {
  const [state, action, pending] = useActionState(deploy, IDLE as DeployState);
  // One idempotency key per previewed set (the parent is keyed by the preview): a retry of
  // this confirmation after a timeout replays the stored outcome instead of applying twice.
  const [operationId] = useState(() => crypto.randomUUID());
  const [note, setNote] = useState("");
  const fieldErrors = state.status === "invalid" ? state.fieldErrors : {};
  return (
    <form onSubmit={submitWith(action)} aria-label="Confirm deployment" className="space-y-3 border-t border-slate-300 pt-3">
      <input type="hidden" name="operation_id" value={operationId} />
      {planIds.map((id) => (
        <input key={id} type="hidden" name="plan_ids" value={id} />
      ))}
      <p className="text-sm text-slate-800">
        Deploying writes the {links} above to the CMS page through the CMS adapter, then verifies them. Every deployment is
        recorded with your name and can be rolled back. A changed selection needs a new dry run.
      </p>
      {(state.status === "invalid" || state.status === "error") && (
        <FormMessage tone="error" message={state.message} focusKey={state} />
      )}
      {state.status === "error" && <IssueList issues={state.issues} planLabels={planLabels} />}
      <FieldError id="deploy-confirm-plans-error" message={fieldErrors.plans} />
      <NoteField idPrefix="deploy" value={note} onChange={setNote} required={false} error={fieldErrors.note} />
      <button type="submit" disabled={pending} className={BUTTON_DANGER}>
        {pending ? "Deploying…" : `Deploy ${links}`}
      </button>
    </form>
  );
}
