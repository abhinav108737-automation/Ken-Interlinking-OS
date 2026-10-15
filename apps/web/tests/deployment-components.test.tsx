import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChangePreviewView } from "@/components/deployments/ChangePreviewView";
import { DEPLOY_SELECT_FORM_ID, DeployFlow, type DeployFlowActions } from "@/components/deployments/DeployFlow";
import { DeploymentActionsPanel, type DeploymentPanelActions } from "@/components/deployments/DeploymentActionsPanel";
import type { DeployState, DeploymentActionState, DryRunState } from "@/lib/deployments";

import { HTML_AFTER, HTML_BEFORE, changePreview, dryRunResult } from "./fixtures/deployments";
import { OTHER_PAGE_ID, PAGE_ID } from "./fixtures/inventory";
import { ANCHOR, OLD_PLAN_ID, PLAN_ID } from "./fixtures/recommendations";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function preview(key: string, overrides: Partial<Extract<DryRunState, { status: "preview" }>> = {}): DryRunState {
  return { status: "preview", key, planIds: [PLAN_ID], result: dryRunResult(), ...overrides };
}

function flowActions(dryRun: DeployFlowActions["dryRun"], deploy?: DeployFlowActions["deploy"]): DeployFlowActions {
  return { dryRun, deploy: deploy ?? vi.fn<DeployFlowActions["deploy"]>(async (): Promise<DeployState> => ({ status: "idle" })) };
}

describe("ChangePreviewView", () => {
  it("renders the snippets as literal text and highlights the anchor", () => {
    const { container } = render(<ChangePreviewView preview={changePreview()} />);
    const pres = container.querySelectorAll("pre");
    expect(pres[0]!.textContent).toBe(HTML_BEFORE);
    expect(pres[1]!.textContent).toBe(HTML_AFTER);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a[data-ken-link-id]")).toBeNull();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
    expect(container.querySelector("mark")).toHaveTextContent(ANCHOR);
    expect(screen.getByText(/\[anchor start\]/)).toBeInTheDocument();
  });

  it("highlights by code points with astral characters, and refuses a mismatched span", () => {
    const text = "🔋⚡ Solid-state 電池 market";
    const { container, unmount } = render(
      <ChangePreviewView preview={changePreview({ block_text: text, anchor_text: "電池 market", anchor_start: 15, anchor_end: 24 })} />,
    );
    expect(container.querySelector("mark")).toHaveTextContent(/^電池 market$/);
    unmount();
    const view = render(<ChangePreviewView preview={changePreview({ anchor_start: 0, anchor_end: 3 })} />);
    expect(view.container.querySelector("mark")).toBeNull();
    expect(screen.getByRole("note")).toHaveTextContent("do not select the anchor text");
  });
});

describe("DeployFlow (one plan)", () => {
  it("dry run → valid preview → deploy with the previewed plans and a form-minted operation id", async () => {
    const dryRun = vi.fn<DeployFlowActions["dryRun"]>(async (): Promise<DryRunState> => preview("p1"));
    const deploy = vi.fn<DeployFlowActions["deploy"]>(async (): Promise<DeployState> => ({ status: "idle" }));
    render(<DeployFlow actions={flowActions(dryRun, deploy)} planId={PLAN_ID} />);
    fireEvent.click(screen.getByRole("button", { name: "Dry run" }));
    await screen.findByRole("heading", { name: "Dry-run result" });
    const sent = dryRun.mock.calls[0]![1] as FormData;
    expect(sent.getAll("plan_ids")).toEqual([PLAN_ID]);
    expect(screen.getByText(/Valid: 1 link can be applied to CMS page cms-42 \(version v7\)/)).toBeInTheDocument();
    const confirm = screen.getByRole("form", { name: "Confirm deployment" });
    fireEvent.change(within(confirm).getByLabelText(/Note/), { target: { value: "Release 12." } });
    fireEvent.click(within(confirm).getByRole("button", { name: "Deploy 1 link" }));
    await waitFor(() => expect(deploy).toHaveBeenCalledTimes(1));
    const body = deploy.mock.calls[0]![1] as FormData;
    expect(body.getAll("plan_ids")).toEqual([PLAN_ID]);
    expect(body.get("note")).toBe("Release 12.");
    expect(body.get("operation_id")).toMatch(UUID);
    // A retry of the same confirmation sends the same id (it replays).
    fireEvent.click(await within(confirm).findByRole("button", { name: "Deploy 1 link" }));
    await waitFor(() => expect(deploy).toHaveBeenCalledTimes(2));
    expect((deploy.mock.calls[1]![1] as FormData).get("operation_id")).toBe(body.get("operation_id"));
  });

  it("each new dry run mints a new operation id for its confirmation", async () => {
    let n = 0;
    const dryRun = vi.fn<DeployFlowActions["dryRun"]>(async (): Promise<DryRunState> => preview(`p${++n}`));
    const deploy = vi.fn<DeployFlowActions["deploy"]>(async (): Promise<DeployState> => ({ status: "idle" }));
    const { container } = render(<DeployFlow actions={flowActions(dryRun, deploy)} planId={PLAN_ID} />);
    const opId = () => container.querySelector<HTMLInputElement>('form[aria-label="Confirm deployment"] input[name="operation_id"]')?.value;
    fireEvent.click(screen.getByRole("button", { name: "Dry run" }));
    await screen.findByRole("form", { name: "Confirm deployment" });
    const first = opId();
    fireEvent.click(screen.getByRole("button", { name: "Dry run" }));
    await waitFor(() => expect(dryRun).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(opId()).not.toBe(first));
    expect(opId()).toMatch(UUID);
  });

  it("an invalid dry run shows labelled issues and no deploy button", async () => {
    const dryRun = vi.fn<DeployFlowActions["dryRun"]>(async (): Promise<DryRunState> =>
      preview("p1", { result: dryRunResult({ valid: false, changes: [], issues: [{ code: "already_linked", plan_id: PLAN_ID }, { code: "from_the_future" as never, plan_id: null }] }) }),
    );
    render(<DeployFlow actions={flowActions(dryRun)} planId={PLAN_ID} planLabels={{ [PLAN_ID]: ANCHOR }} />);
    fireEvent.click(screen.getByRole("button", { name: "Dry run" }));
    const issues = await screen.findByRole("list", { name: "Deployment issues" });
    expect(issues).toHaveTextContent("Already linked");
    expect(issues).toHaveTextContent("Unknown (from_the_future)");
    expect(within(issues).getByRole("link", { name: `“${ANCHOR}”` })).toHaveAttribute("href", `/recommendations/${PLAN_ID}`);
    expect(screen.getByText(/Not deployable/)).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Confirm deployment" })).toBeNull();
  });

  it("a refused deploy explains itself and keeps the confirmation", async () => {
    const dryRun = vi.fn<DeployFlowActions["dryRun"]>(async (): Promise<DryRunState> => preview("p1"));
    const deploy = vi.fn<DeployFlowActions["deploy"]>(async (): Promise<DeployState> => ({
      status: "error",
      code: "conflict",
      message: "Another deployment of this source page is running. Retry later; the plans are unchanged.",
      issues: [{ code: "deployment_in_progress", label: "Deployment in progress", description: null, planId: null }],
    }));
    render(<DeployFlow actions={flowActions(dryRun, deploy)} planId={PLAN_ID} />);
    fireEvent.click(screen.getByRole("button", { name: "Dry run" }));
    fireEvent.click(await screen.findByRole("button", { name: "Deploy 1 link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Retry later");
    expect(screen.getByRole("alert")).toHaveFocus();
    expect(screen.getByRole("form", { name: "Confirm deployment" })).toBeInTheDocument();
  });
});

describe("DeployFlow (inbox selection)", () => {
  function boxes(sources: readonly string[]) {
    return sources.map((source, index) => (
      <input
        key={index}
        type="checkbox"
        name="plan_ids"
        value={index === 0 ? PLAN_ID : OLD_PLAN_ID}
        form={DEPLOY_SELECT_FORM_ID}
        data-source-page-id={source}
        aria-label={`box ${index}`}
        defaultChecked
      />
    ));
  }

  it("refuses ticked plans of several source pages without calling the server", async () => {
    const dryRun = vi.fn<DeployFlowActions["dryRun"]>(async (): Promise<DryRunState> => preview("p1"));
    render(
      <>
        <DeployFlow actions={flowActions(dryRun)} />
        {boxes([PAGE_ID, OTHER_PAGE_ID])}
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Dry run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("One deployment covers one source page");
    expect(dryRun).not.toHaveBeenCalled();
  });

  it("sends the ticked plans of one source page", async () => {
    const dryRun = vi.fn<DeployFlowActions["dryRun"]>(async (): Promise<DryRunState> => preview("p1", { planIds: [PLAN_ID, OLD_PLAN_ID] }));
    render(
      <>
        <DeployFlow actions={flowActions(dryRun)} />
        {boxes([PAGE_ID, PAGE_ID])}
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Dry run" }));
    await waitFor(() => expect(dryRun).toHaveBeenCalledTimes(1));
    expect((dryRun.mock.calls[0]![1] as FormData).getAll("plan_ids")).toEqual([PLAN_ID, OLD_PLAN_ID]);
    expect(await screen.findByRole("button", { name: "Deploy 2 links" })).toBeInTheDocument();
  });
});

describe("DeploymentActionsPanel", () => {
  function panelActions(verify?: DeploymentPanelActions["verify"], rollback?: DeploymentPanelActions["rollback"]): DeploymentPanelActions {
    const idle = async (): Promise<DeploymentActionState> => ({ status: "idle" });
    return { verify: verify ?? vi.fn<DeploymentPanelActions["verify"]>(idle), rollback: rollback ?? vi.fn<DeploymentPanelActions["rollback"]>(idle) };
  }
  const ROLLBACKABLE = [
    { plan_id: PLAN_ID, anchor_text: ANCHOR },
    { plan_id: OLD_PLAN_ID, anchor_text: "battery recycling" },
  ];

  it("re-verify sends the deployment id and shows the outcome", async () => {
    const verify = vi.fn<DeploymentPanelActions["verify"]>(async (): Promise<DeploymentActionState> => ({ status: "success", message: "Verification re-run. Deployment status: Verified." }));
    render(<DeploymentActionsPanel deploymentId="d1" available={{ verify: true, rollback: true }} rollbackable={ROLLBACKABLE} actions={panelActions(verify)} unavailableNote="n" />);
    fireEvent.click(screen.getByRole("button", { name: "Re-verify" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Deployment status: Verified.");
    expect((verify.mock.calls[0]![1] as FormData).get("deployment_id")).toBe("d1");
  });

  it("rollback: reason select, note required for 'other', optional per-link selection, form-minted id", async () => {
    const rollback = vi.fn<DeploymentPanelActions["verify"]>(async (): Promise<DeploymentActionState> => ({ status: "invalid", message: "Correct the highlighted fields.", fieldErrors: { note: "Explain the reason “Other” in the note." } }));
    render(<DeploymentActionsPanel deploymentId="d1" available={{ verify: false, rollback: true }} rollbackable={ROLLBACKABLE} actions={panelActions(undefined, rollback)} unavailableNote="n" />);
    expect(screen.queryByRole("form", { name: "Re-verify" })).toBeNull();
    const form = screen.getByRole("form", { name: "Roll back" });
    const reason = within(form).getByLabelText("Reason (required)");
    expect(within(reason).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Choose a reason…",
      "Verification failed",
      "Editorial reversal",
      "Wrong destination",
      "Broken page",
      "Other (explain in the note)",
    ]);
    expect(within(form).getByLabelText(/Note/)).not.toBeRequired();
    fireEvent.change(reason, { target: { value: "other" } });
    expect(within(form).getByLabelText(/Note \(required/)).toBeRequired();
    fireEvent.click(within(form).getByLabelText("“battery recycling”"));
    // The browser's own constraint check blocks a blank required note before the server is asked.
    fireEvent.click(within(form).getByRole("button", { name: "Roll back" }));
    expect(rollback).not.toHaveBeenCalled();
    fireEvent.change(within(form).getByLabelText(/Note \(required/), { target: { value: "Legal asked." } });
    fireEvent.click(within(form).getByRole("button", { name: "Roll back" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("Correct the highlighted fields.");
    expect(within(form).getByText("Explain the reason “Other” in the note.")).toBeInTheDocument();
    const sent = rollback.mock.calls[0]![1] as FormData;
    expect(sent.get("reason")).toBe("other");
    expect(sent.get("note")).toBe("Legal asked.");
    expect(sent.getAll("plan_ids")).toEqual([OLD_PLAN_ID]);
    expect(sent.getAll("eligible_plan_ids")).toEqual([PLAN_ID, OLD_PLAN_ID]);
    expect(sent.get("deployment_id")).toBe("d1");
    expect(sent.get("operation_id")).toMatch(UUID);
    // The reason and selection survive a refused submit.
    expect(reason).toHaveValue("other");
    fireEvent.click(await within(form).findByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(rollback).toHaveBeenCalledTimes(2));
    expect((rollback.mock.calls[1]![1] as FormData).get("operation_id")).toBe(sent.get("operation_id"));
  });

  const THIRD_PLAN_ID = "0b0c0d0e-0f10-4111-8213-141516171819";
  const THREE = [...ROLLBACKABLE, { plan_id: THIRD_PLAN_ID, anchor_text: "lithium supply" }];
  const SUCCESS: DeploymentActionState = { status: "success", message: "Rollback requested for 1 link. Deployment status: Verified." };
  const UNKNOWN: DeploymentActionState = { status: "error", code: "cms_unavailable", message: "The CMS did not confirm the outcome.", issues: [] };

  function submitRollback(link?: string) {
    const form = screen.getByRole("form", { name: "Roll back" });
    fireEvent.change(within(form).getByLabelText("Reason (required)"), { target: { value: "broken_page" } });
    if (link !== undefined) fireEvent.click(within(form).getByLabelText(link));
    fireEvent.click(within(form).getByRole("button", { name: "Roll back" }));
  }
  const sentAt = (mock: { mock: { calls: unknown[][] } }, call: number) => mock.mock.calls[call]![1] as FormData;

  it("two successive per-plan rollbacks are two operations with different ids", async () => {
    const rollback = vi.fn<DeploymentPanelActions["rollback"]>(async (): Promise<DeploymentActionState> => SUCCESS);
    const panel = (rollbackable: typeof THREE) => (
      <DeploymentActionsPanel deploymentId="d1" available={{ verify: true, rollback: true }} rollbackable={rollbackable} actions={panelActions(undefined, rollback)} unavailableNote="n" />
    );
    const { rerender } = render(panel(THREE));
    submitRollback("“battery recycling”");
    expect(await screen.findByRole("status")).toHaveTextContent("Rollback requested for 1 link.");
    // The page refresh: the deployment is verified again and the rolled-back link is no longer offered.
    rerender(panel([THREE[0]!, THREE[2]!]));
    const form = screen.getByRole("form", { name: "Roll back" });
    // A fresh form: the previous reason and selection are cleared.
    expect(within(form).getByLabelText("Reason (required)")).toHaveValue("");
    submitRollback("“lithium supply”");
    await waitFor(() => expect(rollback).toHaveBeenCalledTimes(2));
    const [first, second] = [sentAt(rollback, 0), sentAt(rollback, 1)];
    expect(first.getAll("plan_ids")).toEqual([OLD_PLAN_ID]);
    expect(second.getAll("plan_ids")).toEqual([THIRD_PLAN_ID]);
    expect(second.getAll("eligible_plan_ids")).toEqual([PLAN_ID, THIRD_PLAN_ID]);
    expect(first.get("operation_id")).toMatch(UUID);
    expect(second.get("operation_id")).toMatch(UUID);
    expect(second.get("operation_id")).not.toBe(first.get("operation_id"));
    // A whole rollback after that is a third operation.
    rerender(panel([THREE[0]!]));
    submitRollback();
    await waitFor(() => expect(rollback).toHaveBeenCalledTimes(3));
    expect(sentAt(rollback, 2).get("operation_id")).not.toBe(second.get("operation_id"));
    expect(sentAt(rollback, 2).getAll("plan_ids")).toEqual([]);
  });

  it("a retry of a failed submission reuses its id; only a success mints a new one", async () => {
    const rollback = vi.fn<DeploymentPanelActions["rollback"]>();
    rollback.mockResolvedValueOnce(UNKNOWN).mockResolvedValueOnce(SUCCESS).mockResolvedValueOnce(SUCCESS);
    render(<DeploymentActionsPanel deploymentId="d1" available={{ verify: true, rollback: true }} rollbackable={THREE} actions={panelActions(undefined, rollback)} unavailableNote="n" />);
    submitRollback("“battery recycling”");
    const form = screen.getByRole("form", { name: "Roll back" });
    expect(await within(form).findByRole("alert")).toHaveTextContent("The CMS did not confirm the outcome.");
    // The same form, unchanged, is resubmitted: it replays the same operation.
    expect(within(form).getByLabelText("Reason (required)")).toHaveValue("broken_page");
    fireEvent.click(within(form).getByRole("button", { name: "Roll back" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Rollback requested");
    expect(sentAt(rollback, 1).get("operation_id")).toBe(sentAt(rollback, 0).get("operation_id"));
    expect(sentAt(rollback, 1).getAll("plan_ids")).toEqual([OLD_PLAN_ID]);
    submitRollback("“lithium supply”");
    await waitFor(() => expect(rollback).toHaveBeenCalledTimes(3));
    expect(sentAt(rollback, 2).get("operation_id")).not.toBe(sentAt(rollback, 1).get("operation_id"));
  });

  it("a recorded rollback refusal shows its issue and the next submission is a new operation", async () => {
    const REFUSED: DeploymentActionState = {
      status: "error",
      message: "The link changed on the page since it was deployed.",
      code: "conflict",
      issues: [{ code: "rollback_conflict", label: "Rollback conflict", description: null, planId: OLD_PLAN_ID }],
    };
    const rollback = vi.fn<DeploymentPanelActions["rollback"]>();
    rollback.mockResolvedValueOnce(REFUSED).mockResolvedValueOnce(SUCCESS);
    render(<DeploymentActionsPanel deploymentId="d1" available={{ verify: true, rollback: true }} rollbackable={THREE} actions={panelActions(undefined, rollback)} unavailableNote="n" />);
    submitRollback("“battery recycling”");
    // The refusal concludes the operation: a fresh form (new id) that still shows the refusal.
    expect(await screen.findByRole("alert")).toHaveTextContent("The link changed on the page");
    expect(screen.getByText("Rollback conflict")).toBeInTheDocument();
    // The API replays a refused id with the same refusal, so a retry after the page is fixed needs a new id.
    submitRollback("“battery recycling”");
    await waitFor(() => expect(rollback).toHaveBeenCalledTimes(2));
    expect(sentAt(rollback, 1).get("operation_id")).toMatch(UUID);
    expect(sentAt(rollback, 1).get("operation_id")).not.toBe(sentAt(rollback, 0).get("operation_id"));
  });

  it("an unknown-outcome error stays visible after the refresh hides the forms", async () => {
    const rollback = vi.fn<DeploymentPanelActions["rollback"]>(async (): Promise<DeploymentActionState> => UNKNOWN);
    const panel = (rolling: boolean) => (
      <DeploymentActionsPanel
        deploymentId="d1"
        available={{ verify: !rolling, rollback: !rolling }}
        rollbackable={ROLLBACKABLE}
        actions={panelActions(undefined, rollback)}
        unavailableNote="No action is available while the outcome is being reconciled."
      />
    );
    const { rerender } = render(panel(false));
    expect(screen.queryByRole("note")).toBeNull();
    submitRollback();
    expect(await screen.findByRole("alert")).toHaveTextContent("The CMS did not confirm the outcome.");
    // The refreshed deployment is `rolling_back`: no action is offered, the outcome is still shown.
    rerender(panel(true));
    expect(screen.queryByRole("form", { name: "Roll back" })).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("The CMS did not confirm the outcome.");
    expect(screen.getByRole("note")).toHaveTextContent("No action is available while the outcome is being reconciled.");
  });

  it("a single-link deployment has no per-link selection", () => {
    render(<DeploymentActionsPanel deploymentId="d1" available={{ verify: true, rollback: true }} rollbackable={ROLLBACKABLE.slice(0, 1)} actions={panelActions()} unavailableNote="n" />);
    expect(within(screen.getByRole("form", { name: "Roll back" })).queryByRole("checkbox")).toBeNull();
  });
});
