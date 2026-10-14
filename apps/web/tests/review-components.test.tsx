import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { EventTable } from "@/components/audit/EventTable";
import { PolicyDocumentView } from "@/components/audit/PolicyDocumentView";
import { LoginForm } from "@/components/auth/LoginForm";
import { SessionHeader } from "@/components/auth/SessionHeader";
import { RecommendationTable } from "@/components/recommendations/RecommendationTable";
import { BULK_FORM_ID, BulkReviewForm } from "@/components/review/BulkReviewForm";
import { ReviewActionTable } from "@/components/review/ReviewActionTable";
import { type ReviewActions, ReviewPanel, type ReviewPanelProps } from "@/components/review/ReviewPanel";
import { SpanPicker } from "@/components/review/SpanPicker";
import { type BulkFormState, REASONS_BY_ACTION, type ReviewFormState, describeFeedbackReason } from "@/lib/review";

import { ANCHOR, BLOCK_TEXT, OLD_PLAN_ID, PLAN_ID, linkPlan } from "./fixtures/recommendations";
import { bulkResult, eventRecord, policyDocument, principal, reviewAction, reviewResult, targetOption } from "./fixtures/review";

const XSS = '<script>window.__pwned = true</script><img src=x onerror="alert(1)">';

function expectNoInjectedMarkup(container: HTMLElement) {
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
}

function hidden(container: HTMLElement, name: string): string | null {
  return container.querySelector<HTMLInputElement>(`input[type="hidden"][name="${name}"]`)?.value ?? null;
}

describe("SpanPicker", () => {
  it("starts on the current anchor and picks a new span by first and last word", () => {
    const { container } = render(<SpanPicker text={BLOCK_TEXT} initialStart={11} initialEnd={37} />);
    expect(hidden(container, "start")).toBe("11");
    expect(hidden(container, "end")).toBe("37");
    const group = screen.getByRole("group", { name: "Words of the source block" });
    fireEvent.click(within(group).getByRole("button", { name: "battery" }));
    expect(screen.getByText(/First word chosen/)).toBeInTheDocument();
    fireEvent.click(within(group).getByRole("button", { name: "market" }));
    expect(hidden(container, "start")).toBe("23");
    expect(hidden(container, "end")).toBe("37");
    expect(container.querySelector("output")).toHaveTextContent("New anchor: “battery market” (23–37)");
    expect(within(group).getByRole("button", { name: "battery" })).toHaveAttribute("aria-pressed", "true");
    expect(within(group).getByRole("button", { name: "global" })).toHaveAttribute("aria-pressed", "false");
  });

  it("selects backwards and a single word", () => {
    const { container } = render(<SpanPicker text={BLOCK_TEXT} initialStart={11} initialEnd={37} />);
    const group = screen.getByRole("group", { name: "Words of the source block" });
    fireEvent.click(within(group).getByRole("button", { name: "market" }));
    fireEvent.click(within(group).getByRole("button", { name: "global" }));
    expect(container.querySelector("output")).toHaveTextContent("“global lithium-ion battery market”");
    fireEvent.click(within(group).getByRole("button", { name: "growing" }));
    fireEvent.click(within(group).getByRole("button", { name: "growing" }));
    expect(container.querySelector("output")).toHaveTextContent("“growing”");
  });

  it("uses code-point offsets with astral characters before the anchor", () => {
    const text = "🔋⚡ Solid-state 電池 market";
    const { container } = render(<SpanPicker text={text} initialStart={3} initialEnd={14} />);
    expect(container.querySelector("output")).toHaveTextContent("“Solid-state” (3–14)");
    const group = screen.getByRole("group", { name: "Words of the source block" });
    fireEvent.click(within(group).getByRole("button", { name: "電池" }));
    fireEvent.click(within(group).getByRole("button", { name: "market" }));
    expect(hidden(container, "start")).toBe("15");
    expect(hidden(container, "end")).toBe("24");
    expect(container.querySelector("output")).toHaveTextContent("“電池 market”");
  });

  it("accepts typed offsets with a live verbatim preview and flags bad spans", () => {
    const { container } = render(<SpanPicker text={BLOCK_TEXT} initialStart={11} initialEnd={37} />);
    fireEvent.change(screen.getByLabelText("Start offset"), { target: { value: "4" } });
    expect(container.querySelector("output")).toHaveTextContent("“global lithium-ion battery market”");
    fireEvent.change(screen.getByLabelText("End offset (exclusive)"), { target: { value: "11" } });
    expect(screen.getByText(/cannot start or end with whitespace/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("End offset (exclusive)"), { target: { value: "3" } });
    expect(screen.getByText(/end must come after the start/)).toBeInTheDocument();
  });

  it("is one tab stop; arrow keys move between words", () => {
    render(<SpanPicker text={BLOCK_TEXT} initialStart={11} initialEnd={37} />);
    const buttons = within(screen.getByRole("group", { name: "Words of the source block" })).getAllByRole("button");
    expect(buttons.filter((button) => button.tabIndex === 0)).toHaveLength(1);
    const current = buttons.find((button) => button.tabIndex === 0)!;
    expect(current).toHaveTextContent("lithium-ion");
    current.focus();
    fireEvent.keyDown(current, { key: "ArrowRight" });
    expect(document.activeElement).toHaveTextContent("battery");
    fireEvent.keyDown(document.activeElement!, { key: "Home" });
    expect(document.activeElement).toHaveTextContent("The");
  });

  it("renders markup in block text as literal text", () => {
    const { container } = render(<SpanPicker text={`Intro ${XSS} outro`} initialStart={0} initialEnd={5} />);
    expectNoInjectedMarkup(container);
    expect(container.textContent).toContain("<script>");
  });
});

function panelProps(overrides: Partial<ReviewPanelProps> = {}, actions: Partial<ReviewActions> = {}): ReviewPanelProps {
  const idle = vi.fn(async (): Promise<ReviewFormState> => ({ status: "idle" }));
  return {
    planId: PLAN_ID,
    status: "awaiting_approval",
    statusLabel: "Awaiting approval",
    available: { approve: true, reject: true, edit: true },
    blockText: BLOCK_TEXT,
    contextHash: "a".repeat(64),
    anchorStart: 11,
    anchorEnd: 37,
    targetOptions: [targetOption()],
    actions: { approve: idle, reject: idle, editAnchor: idle, replaceTarget: idle, ...actions },
    ...overrides,
  };
}

describe("ReviewPanel", () => {
  it("offers approve, reject, edit and replace with each action's own reasons", () => {
    render(<ReviewPanel {...panelProps()} />);
    for (const [name, action] of [
      ["Approve", "approve"],
      ["Reject", "reject"],
      ["Edit the anchor or placement", "edit_anchor"],
      ["Replace the destination", "replace_target"],
    ] as const) {
      const form = screen.getByRole("form", { name });
      const labels = within(within(form).getAllByRole("group").find((g) => g.tagName === "FIELDSET" && /Reasons/.test(g.textContent ?? ""))!)
        .getAllByRole("checkbox")
        .map((box) => box.getAttribute("value"));
      expect(labels).toEqual([...REASONS_BY_ACTION[action]]);
      expect(within(form).getByDisplayValue(PLAN_ID)).toHaveAttribute("name", "plan_id");
      expect(within(form).getByDisplayValue("awaiting_approval")).toHaveAttribute("name", "expected_status");
    }
    expect(screen.getByRole("form", { name: "Reject" })).toHaveTextContent(describeFeedbackReason("irrelevant_destination"));
    expect(screen.getByRole("form", { name: "Reject" })).not.toHaveTextContent("Good as is");
    expect(screen.getByRole("form", { name: "Edit the anchor or placement" }).querySelector('input[name="block_context_hash"]')).toHaveValue(
      "a".repeat(64),
    );
  });

  it("only shows reversal for an approved plan, and nothing for a terminal one", () => {
    const { rerender } = render(<ReviewPanel {...panelProps({ status: "approved", available: { approve: false, reject: true, edit: true } })} />);
    expect(screen.queryByRole("form", { name: "Approve" })).toBeNull();
    expect(screen.getByRole("form", { name: "Reverse the approval (reject)" })).toBeInTheDocument();
    rerender(<ReviewPanel {...panelProps({ status: "rejected", statusLabel: "Rejected", available: { approve: false, reject: false, edit: false } })} />);
    expect(screen.queryAllByRole("form")).toHaveLength(0);
    expect(screen.getByRole("note")).toHaveTextContent("This recommendation is rejected; no review action applies to it.");
  });

  it("makes the note required once 'Other' is chosen", () => {
    render(<ReviewPanel {...panelProps()} />);
    const reject = screen.getByRole("form", { name: "Reject" });
    const note = within(reject).getByRole("textbox");
    expect(note).not.toBeRequired();
    fireEvent.click(within(reject).getByLabelText("Other (explain in the note)"));
    expect(note).toBeRequired();
  });

  it("submits the form data to the server action and focuses a refusal", async () => {
    const reject = vi.fn(async (_state: ReviewFormState, form: FormData): Promise<ReviewFormState> => {
      expect(form.getAll("reasons")).toEqual(["over_linking"]);
      expect(form.get("note")).toBe("Busy paragraph");
      return { status: "error", message: "This recommendation changed since you loaded it.", code: "conflict" };
    });
    render(<ReviewPanel {...panelProps({}, { reject })} />);
    const form = screen.getByRole("form", { name: "Reject" });
    fireEvent.click(within(form).getByLabelText("Over-linking"));
    fireEvent.change(within(form).getByRole("textbox"), { target: { value: "Busy paragraph" } });
    await act(async () => {
      fireEvent.click(within(form).getByRole("button", { name: "Reject" }));
    });
    await waitFor(() => expect(within(form).getByRole("alert")).toHaveTextContent("changed since you loaded it"));
    expect(reject).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(within(form).getByRole("alert"));
    // A refused submit keeps the reviewer's input.
    expect(within(form).getByLabelText("Over-linking")).toBeChecked();
  });

  it("keeps the success message after the status changes and the form disappears", async () => {
    const approve = vi.fn(async (): Promise<ReviewFormState> => ({ status: "success", message: "Approved. Nothing is deployed yet.", result: reviewResult() }));
    const { rerender } = render(<ReviewPanel {...panelProps({}, { approve })} />);
    await act(async () => {
      fireEvent.click(within(screen.getByRole("form", { name: "Approve" })).getByRole("button", { name: "Approve" }));
    });
    await waitFor(() => expect(screen.getByText("Approved. Nothing is deployed yet.")).toBeInTheDocument());
    rerender(<ReviewPanel {...panelProps({ status: "approved", available: { approve: false, reject: true, edit: true } }, { approve })} />);
    expect(screen.getByText("Approved. Nothing is deployed yet.").closest("p")).toHaveAttribute("role", "status");
  });

  it("lists replacement destinations as text and handles none or a failed load", () => {
    const { rerender, container } = render(<ReviewPanel {...panelProps({ targetOptions: [targetOption({ title: XSS })] })} />);
    const form = screen.getByRole("form", { name: "Replace the destination" });
    expect(within(form).getByRole("radio")).toHaveAttribute("value", targetOption().page_id);
    expect(form).toHaveTextContent("Relevance 0.710");
    expectNoInjectedMarkup(container);
    rerender(<ReviewPanel {...panelProps({ targetOptions: [] })} />);
    expect(screen.getByRole("form", { name: "Replace the destination" })).toHaveTextContent("No other eligible destination");
    rerender(<ReviewPanel {...panelProps({ targetOptions: null })} />);
    expect(screen.getByRole("form", { name: "Replace the destination" })).toHaveTextContent("could not be loaded");
  });
});

describe("BulkReviewForm", () => {
  it("submits the table's checkboxes through the form attribute and lists per-plan results", async () => {
    const action = vi.fn(async (_state: BulkFormState, form: FormData): Promise<BulkFormState> => {
      expect(form.getAll("plan_ids")).toEqual([PLAN_ID]);
      expect(form.get("bulk_action")).toBe("approve");
      return { status: "done", message: "1 of 2 approved; 1 not changed (see below).", result: bulkResult() };
    });
    render(
      <>
        <BulkReviewForm action={action} />
        <RecommendationTable
          plans={[linkPlan({ status: "awaiting_approval" }), linkPlan({ id: OLD_PLAN_ID, status: "approved" })]}
          total={2}
          selectFormId={BULK_FORM_ID}
        />
      </>,
    );
    const boxes = screen.getAllByRole("checkbox", { name: /for bulk review/ });
    expect(boxes).toHaveLength(1);
    expect(boxes[0]).toHaveAttribute("form", BULK_FORM_ID);
    fireEvent.click(screen.getByRole("button", { name: "Select all on this page" }));
    expect(boxes[0]).toBeChecked();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Approve selected" }));
    });
    const results = await screen.findByRole("list", { name: "Bulk review results" });
    const items = within(results).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent(new RegExp(`Done.*${PLAN_ID}.*Approved`));
    expect(items[1]).toHaveTextContent("Not changed");
    expect(items[1]).toHaveTextContent("Source page changed (stale)");
  });

  it("switches to reject reasons, which are required", () => {
    render(<BulkReviewForm action={vi.fn()} />);
    expect(screen.getByRole("group", { name: "Reasons (optional)" })).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Reject"));
    expect(screen.getByRole("group", { name: "Reasons (at least one)" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Good as is")).toBeNull();
    expect(screen.getByRole("button", { name: "Reject selected" })).toBeInTheDocument();
  });

  it("adds no selection column without a bulk form", () => {
    render(<RecommendationTable plans={[linkPlan({ status: "awaiting_approval" })]} total={1} />);
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});

describe("text-only rendering", () => {
  it("review actions: notes, anchors and actors stay literal", () => {
    const { container } = render(
      <ReviewActionTable
        caption="Review actions"
        actions={[reviewAction({ note: XSS, anchor_before: XSS, anchor_after: `${ANCHOR} ${XSS}`, actor: `user:${XSS}`, reasons: ["other", "over_linking"] })]}
      />,
    );
    expectNoInjectedMarkup(container);
    expect(screen.getAllByText(XSS, { exact: false }).length).toBeGreaterThanOrEqual(3);
    expect(container).toHaveTextContent("Other (explain in the note)");
    expect(container).toHaveTextContent("Over-linking");
  });

  it("events: the payload is compact JSON text", () => {
    const { container } = render(<EventTable events={[eventRecord({ payload: { note: "<script>alert(1)</script>", token: "s3cret" } })]} total={1} />);
    expectNoInjectedMarkup(container);
    expect(container.querySelector("code")).toHaveTextContent('{"note":"<script>alert(1)</script>","token":"[withheld]"}');
    expect(container).toHaveTextContent("Link approved");
  });

  it("policies: identity facts and content as key/value text", () => {
    const { container } = render(<PolicyDocumentView policy={policyDocument({ content: { name: XSS, password: "x", weights: { semantic: 0.5 } } })} />);
    expectNoInjectedMarkup(container);
    expect(screen.getByRole("heading", { name: "Ranking policy" })).toBeInTheDocument();
    expect(container).toHaveTextContent("policies/ranking/ranking_weights.v1.yaml");
    expect(container).toHaveTextContent("b".repeat(64));
    const table = screen.getByRole("table", { name: /Ranking policy content/ });
    expect(within(table).getByRole("rowheader", { name: "password" }).closest("tr")).toHaveTextContent("[withheld]");
    expect(within(table).getByRole("rowheader", { name: "weights" }).closest("tr")).toHaveTextContent('{"semantic":0.5}');
  });

  it("session header: user, role and sign-out; signed out shows sign in", () => {
    const logout = vi.fn(async () => {});
    const { container, rerender } = render(<SessionHeader user={principal("operator_admin", { display_name: XSS })} logoutAction={logout} />);
    expectNoInjectedMarkup(container);
    expect(container).toHaveTextContent("Operator admin");
    expect(container).toHaveTextContent("(jane)");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    rerender(<SessionHeader user={null} logoutAction={logout} />);
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  });
});

describe("LoginForm", () => {
  it("has labelled fields, carries next, and shows a refusal without echoing the password", async () => {
    const action = vi.fn(async (_state: { error: string | null; username: string }, form: FormData) => {
      expect(form.get("next")).toBe("/jobs");
      return { error: "Sign-in failed: the username or password was not accepted.", username: String(form.get("username")) };
    });
    render(<LoginForm action={action} next="/jobs" />);
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "jane" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "hunter2hunter2" } });
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "current-password");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("not accepted");
    expect(document.body.textContent).not.toContain("hunter2hunter2");
  });
});
