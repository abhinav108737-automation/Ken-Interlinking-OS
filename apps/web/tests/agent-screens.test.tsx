import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AgentRunDetailPage from "@/app/agents/[runId]/page";
import AgentRunNotFound from "@/app/agents/[runId]/not-found";
import AgentRunsPage from "@/app/agents/page";
import RecommendationDetailPage from "@/app/recommendations/[planId]/page";
import type { ApiError, ApiResult } from "@/lib/api-client";

import {
  AGENT_JOB_ID,
  AGENT_RUN_ID,
  CORRELATION_ID,
  TRACE_ID,
  agentPlanReview,
  agentRunDetail,
  agentRunSummary,
  agentStep,
  toolCall,
} from "./fixtures/agents";
import { PAGE_ID, list } from "./fixtures/inventory";
import { OLD_PLAN_ID, PLAN_ID, linkPlanDetail } from "./fixtures/recommendations";

const api = vi.hoisted(() => ({
  listAgentRuns: vi.fn(),
  getAgentRun: vi.fn(),
  listAgentPlanReviews: vi.fn(),
  getLinkPlan: vi.fn(),
}));

const nav = vi.hoisted(() => ({
  notFound: vi.fn(() => {
    throw new Error("NEXT_NOT_FOUND");
  }),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

vi.mock("next/navigation", async (importOriginal) => ({
  ...(await importOriginal<typeof import("next/navigation")>()),
  notFound: nav.notFound,
}));

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, status: 200 };
}

function fail(error: Partial<ApiError> & Pick<ApiError, "kind">): ApiResult<never> {
  return { ok: false, error: { path: "/agents/runs", message: "It failed.", ...error } };
}

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  nav.notFound.mockClear();
});

async function renderPage(element: Promise<React.ReactElement>) {
  return render(await element);
}

const XSS = '<script>window.__pwned = true</script><img src=x onerror="alert(1)">';

function expectNoInjectedMarkup(container: HTMLElement) {
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
}

describe("/agents", () => {
  it("lists runs with source, trigger, status, outcome, triage, hand-offs and duration", async () => {
    api.listAgentRuns.mockResolvedValue(ok(list([agentRunSummary()], { total: 120, has_more: true })));
    await renderPage(AgentRunsPage({ searchParams: Promise.resolve({}) }));
    expect(api.listAgentRuns).toHaveBeenCalledWith({ status: null, outcome: null, limit: 50, offset: 0 });
    const table = screen.getByRole("table", { name: /agent runs/i });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Started",
      "Source page",
      "Trigger",
      "Status",
      "Outcome",
      "Triage",
      "Handed off",
      "Duration",
    ]);
    const row = within(table).getAllByRole("row")[1]!;
    expect(within(row).getByRole("link", { name: /2026-10-14 10:00:00 UTC — view agent run/ })).toHaveAttribute(
      "href",
      `/agents/${AGENT_RUN_ID}`,
    );
    expect(within(row).getByRole("link", { name: "https://www.example.com/reports/widgets" })).toHaveAttribute(
      "href",
      `/inventory/pages/${PAGE_ID}`,
    );
    const cells = within(row).getAllByRole("cell").map((c) => c.textContent);
    expect(cells).toEqual([
      "https://www.example.com/reports/widgetsversion 2",
      "Batch",
      "!Awaiting approval",
      "✓Recommend",
      "Optimize (review proposed plans)",
      "1",
      "3.5 s",
    ]);
    expect(screen.getByRole("link", { name: /next/i })).toHaveAttribute("href", "/agents?offset=50");
  });

  it("passes the status and outcome filters and keeps them across pages", async () => {
    api.listAgentRuns.mockResolvedValue(ok(list([agentRunSummary()], { total: 80, offset: 50, has_more: false })));
    await renderPage(
      AgentRunsPage({ searchParams: Promise.resolve({ status: "completed", outcome: "needs_human_review", offset: "50" }) }),
    );
    expect(api.listAgentRuns).toHaveBeenCalledWith({
      status: "completed",
      outcome: "needs_human_review",
      limit: 50,
      offset: 50,
    });
    expect(screen.getByLabelText("Status")).toHaveValue("completed");
    expect(screen.getByLabelText("Outcome")).toHaveValue("needs_human_review");
    const prev = new URL(screen.getByRole("link", { name: /previous/i }).getAttribute("href")!, "http://x");
    expect(prev.pathname).toBe("/agents");
    expect(prev.searchParams.get("status")).toBe("completed");
    expect(prev.searchParams.get("outcome")).toBe("needs_human_review");
    expect(screen.getByRole("link", { name: "Clear filters" })).toHaveAttribute("href", "/agents");
  });

  it("ignores filter values outside the contract", async () => {
    api.listAgentRuns.mockResolvedValue(ok(list([])));
    await renderPage(AgentRunsPage({ searchParams: Promise.resolve({ status: "approved", outcome: "<x>" }) }));
    expect(api.listAgentRuns).toHaveBeenCalledWith(expect.objectContaining({ status: null, outcome: null }));
  });

  it("shows the 'nothing yet' empty state when no run exists", async () => {
    api.listAgentRuns.mockResolvedValue(ok(list([])));
    await renderPage(AgentRunsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("status")).toHaveTextContent("No agent runs yet — run make agents after make recommend.");
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("explains an empty filtered result", async () => {
    api.listAgentRuns.mockResolvedValue(ok(list([])));
    await renderPage(AgentRunsPage({ searchParams: Promise.resolve({ status: "failed" }) }));
    expect(screen.getByRole("status")).toHaveTextContent("No agent runs match these filters.");
  });

  it("renders unknown enum values as explicit Unknown labels", async () => {
    api.listAgentRuns.mockResolvedValue(
      ok(
        list([
          agentRunSummary({
            status: "paused" as never,
            outcome: "escalate" as never,
            triage_decision: "teleport" as never,
            trigger: "cron" as never,
          }),
          agentRunSummary({ id: OLD_PLAN_ID, status: "running", outcome: null, triage_decision: null, finished_at: null }),
        ]),
      ),
    );
    await renderPage(AgentRunsPage({ searchParams: Promise.resolve({}) }));
    const [, unknownRow, runningRow] = within(screen.getByRole("table", { name: /agent runs/i })).getAllByRole("row");
    expect(unknownRow).toHaveTextContent("Unknown (paused)");
    expect(unknownRow).toHaveTextContent("Unknown (escalate)");
    expect(unknownRow).toHaveTextContent("Unknown (teleport)");
    expect(unknownRow).toHaveTextContent("Unknown (cron)");
    expect(runningRow).toHaveTextContent("Running");
    expect(runningRow).toHaveTextContent("Pending");
    expect(runningRow).toHaveTextContent("In progress");
  });

  it("renders an API error without crashing, and offers no mutating controls", async () => {
    api.listAgentRuns.mockResolvedValue(fail({ kind: "network" }));
    const { container } = await renderPage(AgentRunsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("API unreachable: Agent runs unavailable");
    expect(screen.queryByRole("button", { name: /run|start|approve|reject|retry/i })).toBeNull();
    for (const form of container.querySelectorAll("form")) expect(form.getAttribute("method")).toBe("get");
  });
});

describe("/agents/[runId]", () => {
  async function renderDetail() {
    return renderPage(AgentRunDetailPage({ params: Promise.resolve({ runId: AGENT_RUN_ID }) }));
  }

  it("shows the run header, ids, timings and the configuration snapshot", async () => {
    api.getAgentRun.mockResolvedValue(ok(agentRunDetail()));
    await renderDetail();
    expect(api.getAgentRun).toHaveBeenCalledWith(AGENT_RUN_ID);
    const header = screen.getByRole("heading", { level: 1, name: "Agent run" }).parentElement!;
    expect(within(header).getByText("Awaiting approval")).toBeInTheDocument();
    expect(within(header).getByText("Recommend")).toBeInTheDocument();
    expect(header).toHaveTextContent(/Nothing is approved or deployed/);

    const summary = screen.getByRole("heading", { level: 2, name: "Summary" }).closest("section")!;
    const fact = (term: string) => within(summary).getByText(term, { selector: "dt" }).nextElementSibling;
    expect(within(summary).getByRole("link", { name: "https://www.example.com/reports/widgets" })).toHaveAttribute(
      "href",
      `/inventory/pages/${PAGE_ID}`,
    );
    expect(fact("Source version")).toHaveTextContent("v2");
    expect(fact("Trigger")).toHaveTextContent("Batch");
    expect(fact("Triage")).toHaveTextContent("Optimize (review proposed plans) optimize");
    expect(fact("Started")).toHaveTextContent("2026-10-14 10:00:00 UTC");
    expect(fact("Finished")).toHaveTextContent("2026-10-14 10:00:03 UTC");
    expect(fact("Duration")).toHaveTextContent("3.5 s");
    expect(fact("Graph version")).toHaveTextContent("graph-v1");
    expect(fact("Correlation id")).toHaveTextContent(CORRELATION_ID);
    expect(fact("Trace id")).toHaveTextContent(TRACE_ID);
    expect(within(summary).getByRole("link", { name: AGENT_JOB_ID })).toHaveAttribute("href", `/jobs/${AGENT_JOB_ID}`);
    const handedOff = within(summary).getByRole("list", { name: "Handed-off plans" });
    expect(within(handedOff).getByRole("link", { name: PLAN_ID })).toHaveAttribute("href", `/recommendations/${PLAN_ID}`);

    const config = screen.getByRole("table", { name: /run configuration/i });
    expect(within(config).getByRole("rowheader", { name: "agents_policy_version" }).nextElementSibling).toHaveTextContent("1");
    expect(within(config).getByRole("rowheader", { name: "critic" }).nextElementSibling).toHaveTextContent(
      '{"use_model":true}',
    );
    expect(screen.getByText("No review notes.")).toBeInTheDocument();
  });

  it("shows a failed run's error code and review notes as text", async () => {
    api.getAgentRun.mockResolvedValue(
      ok(
        agentRunDetail({
          status: "failed",
          outcome: "failed",
          error_code: "handoff_conflict",
          handed_off_plan_ids: [],
          review_notes: ["Plan c1 expired before hand-off.", XSS],
        }),
      ),
    );
    const { container } = await renderDetail();
    const header = screen.getByRole("heading", { level: 1 }).parentElement!;
    expect(header).toHaveTextContent("Error: Hand-off conflict (a plan changed status concurrently) handoff_conflict");
    expect(within(header).getAllByText("Failed")).toHaveLength(2);
    const notes = screen.getByRole("list", { name: "Review notes" });
    expect(within(notes).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Plan c1 expired before hand-off.",
      XSS,
    ]);
    expect(screen.getByText("Handed off", { selector: "dt" }).nextElementSibling).toHaveTextContent("No plans");
    expectNoInjectedMarkup(container);
  });

  it("lists plan reviews with verdict, reasons, hand-off, fallback and model", async () => {
    api.getAgentRun.mockResolvedValue(ok(agentRunDetail()));
    await renderDetail();
    const reviews = screen.getByRole("table", { name: /plan reviews/i });
    const [accepted, rejected] = within(reviews).getAllByRole("row").slice(1);
    expect(within(accepted!).getByRole("link", { name: PLAN_ID })).toHaveAttribute("href", `/recommendations/${PLAN_ID}`);
    expect(accepted).toHaveTextContent("Accept");
    expect(accepted).toHaveTextContent("Natural anchor text");
    expect(accepted).toHaveTextContent("Relevant destination");
    expect(accepted).toHaveTextContent("The anchor reads naturally");
    expect(accepted).toHaveTextContent("Yes — sent to human review");
    expect(accepted).toHaveTextContent("qwen3:14b");
    expect(within(accepted!).queryByText("Fallback")).toBeNull();
    expect(within(rejected!).getByRole("link", { name: OLD_PLAN_ID })).toBeInTheDocument();
    expect(rejected).toHaveTextContent("Reject");
    expect(rejected).toHaveTextContent("Weak relevance");
    expect(rejected).toHaveTextContent("No explanation");
    expect(rejected).toHaveTextContent("No — left as proposed");
    expect(within(rejected!).getByText("Fallback")).toBeInTheDocument();
    expect(rejected).toHaveTextContent("Model timed out");
    expect(rejected).toHaveTextContent("No model answered");
    // Each review links back to the run that produced it.
    expect(within(accepted!).getByRole("link", { name: /view agent run/i })).toHaveAttribute(
      "href",
      `/agents/${AGENT_RUN_ID}`,
    );
  });

  it("renders a model explanation containing <script> as literal text", async () => {
    api.getAgentRun.mockResolvedValue(ok(agentRunDetail({ reviews: [agentPlanReview({ explanation: XSS })] })));
    const { container } = await renderDetail();
    expectNoInjectedMarkup(container);
    const reviews = screen.getByRole("table", { name: /plan reviews/i });
    expect(within(reviews).getByText(XSS)).toBeInTheDocument();
    expect(within(reviews).getByText(XSS).tagName).toBe("P");
  });

  it("renders the step timeline in seq order with tool calls, fallbacks and output as text", async () => {
    api.getAgentRun.mockResolvedValue(
      ok(
        agentRunDetail({
          steps: [
            agentStep({
              seq: 7,
              node: "policy_gate",
              tool_calls: [toolCall({ tool: "hand_off_plans", permission: "review", duration_ms: 40, attempt: 2 })],
              output: { handed_off: 1 },
            }),
            agentStep(),
            agentStep({
              seq: 5,
              node: "critique",
              status: "failed",
              attempt: 2,
              model_role: "reasoning",
              model: null,
              fallback_used: true,
              fallback_reason: "model_timeout",
              error_code: "timeout",
              tool_calls: [toolCall({ tool: "check_plan", ok: false, error_code: "tool_failed" })],
              output: { note: XSS },
            }),
          ],
        }),
      ),
    );
    const { container } = await renderDetail();
    const timeline = screen.getByRole("list", { name: "Step timeline" });
    const steps = within(timeline).getAllByRole("listitem");
    expect(steps.map((li) => li.querySelector("h3")?.textContent)).toEqual([
      "0. Load page load_page",
      "5. Critique critique",
      "7. Policy gate policy_gate",
    ]);

    const load = steps[0]!;
    expect(within(load).getByText("Succeeded")).toBeInTheDocument();
    expect(within(load).getByText("Model role", { selector: "dt" }).nextElementSibling).toHaveTextContent("None (deterministic node)");
    expect(within(load).getByText("Duration", { selector: "dt" }).nextElementSibling).toHaveTextContent("250 ms");
    const loadTools = within(load).getByRole("table", { name: /tool calls of step 0/i });
    const loadRow = within(loadTools).getAllByRole("row")[1]!;
    expect(within(loadRow).getByRole("rowheader")).toHaveTextContent("Get page get_page");
    expect(within(loadRow).getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "Read",
      "✓ OK",
      "12 ms",
      "1",
      `{"page_id":"${PAGE_ID}","found":true}`,
    ]);

    const critique = steps[1]!;
    // The step's status badge and its failed tool call.
    expect(within(critique).getAllByText("Failed")).toHaveLength(2);
    expect(within(critique).getByText("Attempt", { selector: "dt" }).nextElementSibling).toHaveTextContent("2");
    expect(within(critique).getByText("Model role", { selector: "dt" }).nextElementSibling).toHaveTextContent("Reasoning");
    const model = within(critique).getByText("Model", { selector: "dt" }).nextElementSibling!;
    expect(within(model as HTMLElement).getByText("Fallback")).toBeInTheDocument();
    expect(model).toHaveTextContent("Model timed out");
    expect(within(critique).getByText("Error code", { selector: "dt" }).nextElementSibling).toHaveTextContent("Timed out timeout");
    const failedCall = within(within(critique).getByRole("table")).getAllByRole("row")[1]!;
    expect(failedCall).toHaveTextContent("Failed");
    expect(failedCall).toHaveTextContent("A tool call failed tool_failed");
    const output = critique.querySelector("pre")!;
    expect(output.textContent).toBe(JSON.stringify({ note: XSS }));
    expect(output.children).toHaveLength(0);

    const gate = steps[2]!;
    const gateRow = within(within(gate).getByRole("table")).getAllByRole("row")[1]!;
    expect(gateRow).toHaveTextContent("Hand off plans");
    expect(gateRow).toHaveTextContent("Review (hands plans to the human queue)");
    expect(within(gateRow).getAllByRole("cell")[3]).toHaveTextContent("2");
    expect(gate.querySelector("pre")).toHaveTextContent('{"handed_off":1}');
    expectNoInjectedMarkup(container);
  });

  it("renders unknown node, step status, tool and verdict values as Unknown labels", async () => {
    api.getAgentRun.mockResolvedValue(
      ok(
        agentRunDetail({
          steps: [
            agentStep({
              node: "self_improve" as never,
              status: "retrying" as never,
              model_role: "vision" as never,
              fallback_used: true,
              fallback_reason: "gpu_on_fire" as never,
              error_code: "cosmic_ray" as never,
              tool_calls: [toolCall({ tool: "rm_rf" as never, permission: "write" as never })],
            }),
          ],
          reviews: [agentPlanReview({ verdict: "maybe" as never, reasons: ["vibes" as never] })],
        }),
      ),
    );
    await renderDetail();
    const step = within(screen.getByRole("list", { name: "Step timeline" })).getAllByRole("listitem")[0]!;
    for (const text of [
      "Unknown (self_improve)",
      "Unknown (retrying)",
      "Unknown (vision)",
      "Unknown (gpu_on_fire)",
      "Unknown (cosmic_ray)",
      "Unknown (rm_rf)",
      "Unknown (write)",
    ]) {
      expect(step).toHaveTextContent(text);
    }
    const reviews = screen.getByRole("table", { name: /plan reviews/i });
    expect(reviews).toHaveTextContent("Unknown (maybe)");
    expect(reviews).toHaveTextContent("Unknown (vibes)");
  });

  it("shows empty states for a run with no steps, reviews or configuration", async () => {
    api.getAgentRun.mockResolvedValue(
      ok(
        agentRunDetail({
          status: "running",
          outcome: null,
          triage_decision: null,
          finished_at: null,
          source_page_version: null,
          handed_off_plan_ids: [],
          steps: [],
          reviews: [],
          config_snapshot: {},
        }),
      ),
    );
    await renderDetail();
    expect(screen.getByText("No steps are recorded for this run.")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /plan reviews/i })).toHaveTextContent("This run reviewed no plans.");
    expect(screen.getByRole("table", { name: /run configuration/i })).toHaveTextContent(
      "No configuration was recorded for this run.",
    );
    expect(screen.getByText("Duration", { selector: "dt" }).nextElementSibling).toHaveTextContent("In progress");
    expect(screen.getByText("Triage", { selector: "dt" }).nextElementSibling).toHaveTextContent("— (not reached)");
    expect(screen.getByText("Source version", { selector: "dt" }).nextElementSibling).toHaveTextContent("— (not loaded)");
    expect(within(screen.getByRole("heading", { level: 1 }).parentElement!).getByText("Pending")).toBeInTheDocument();
  });

  it("calls notFound for a 404, a 422 and a malformed id", async () => {
    for (const result of [
      fail({ kind: "api_error", status: 404, code: "not_found", path: "/agents/runs/{run_id}" }),
      fail({ kind: "api_error", status: 422, code: "validation_failed", path: "/agents/runs/{run_id}" }),
      fail({ kind: "invalid_argument", path: "/agents/runs/{run_id}" }),
    ]) {
      api.getAgentRun.mockResolvedValueOnce(result);
      await expect(renderDetail()).rejects.toThrow("NEXT_NOT_FOUND");
    }
    expect(nav.notFound).toHaveBeenCalledTimes(3);
  });

  it("renders other failures as an error notice", async () => {
    api.getAgentRun.mockResolvedValue(fail({ kind: "invalid_body", status: 200, path: "/agents/runs/{run_id}" }));
    await renderDetail();
    expect(screen.getByRole("alert")).toHaveTextContent("API response did not match the contract: Agent run unavailable");
    expect(nav.notFound).not.toHaveBeenCalled();
  });

  it("has a not-found screen that links back to the run list", () => {
    render(<AgentRunNotFound />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Agent run not found");
    expect(screen.getByRole("link", { name: "Back to agent runs" })).toHaveAttribute("href", "/agents");
  });

  it("offers no mutating controls", async () => {
    api.getAgentRun.mockResolvedValue(ok(agentRunDetail()));
    const { container } = await renderDetail();
    expect(screen.queryByRole("button")).toBeNull();
    expect(container.querySelector("form")).toBeNull();
  });
});

describe("/recommendations/[planId] agent review panel", () => {
  async function renderPlan() {
    return renderPage(RecommendationDetailPage({ params: Promise.resolve({ planId: PLAN_ID }) }));
  }

  beforeEach(() => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail()));
  });

  function panel() {
    return screen.getByRole("heading", { level: 2, name: "Agent review" }).closest("section")!;
  }

  it("shows the plan's agent review without a plan column, explanation as text", async () => {
    api.listAgentPlanReviews.mockResolvedValue(
      ok([agentPlanReview({ verdict: "uncertain", explanation: XSS, reasons: ["low_confidence"] })]),
    );
    const { container } = await renderPlan();
    expect(api.listAgentPlanReviews).toHaveBeenCalledWith(PLAN_ID);
    const table = within(panel()).getByRole("table", { name: /agent review of this plan/i });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Verdict",
      "Reasons",
      "Explanation",
      "Handed off",
      "Model",
      "Reviewed",
    ]);
    expect(table).toHaveTextContent("Uncertain");
    expect(table).toHaveTextContent("Low confidence");
    expect(within(table).getByText(XSS)).toBeInTheDocument();
    expectNoInjectedMarkup(container);
  });

  it("says when no agent has reviewed the plan", async () => {
    api.listAgentPlanReviews.mockResolvedValue(ok([]));
    await renderPlan();
    expect(within(panel()).getByRole("status")).toHaveTextContent("Not reviewed by an agent yet.");
    expect(within(panel()).getByRole("link", { name: "agent runs" })).toHaveAttribute("href", "/agents");
  });

  it("treats a failing review fetch as a non-fatal notice", async () => {
    api.listAgentPlanReviews.mockResolvedValue(fail({ kind: "timeout", path: "/agents/plans/{plan_id}/reviews" }));
    await renderPlan();
    expect(within(panel()).getByRole("alert")).toHaveTextContent("API timed out: Agent review unavailable");
    expect(screen.getByRole("heading", { level: 2, name: "Placement preview" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Status history" })).toBeInTheDocument();
    expect(nav.notFound).not.toHaveBeenCalled();
  });
});
