import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import EvaluationsPage from "@/app/learning/evaluations/page";
import LearningPage from "@/app/learning/page";
import ProposalDetailPage from "@/app/learning/proposals/[proposalId]/page";
import ProposalsPage from "@/app/learning/proposals/page";
import SkillDetailPage from "@/app/learning/skills/[name]/page";
import SkillsPage from "@/app/learning/skills/page";
import type { ApiError, ApiResult, Role } from "@/lib/api-client";

import { list } from "./fixtures/inventory";
import {
  PROPOSAL_ID,
  corpusBanner,
  evaluationSummary,
  learningOverview,
  proposalDetail,
  proposalSummary,
  skillDetail,
  skillSummary,
} from "./fixtures/learning";
import { principal } from "./fixtures/review";

const api = vi.hoisted(() => ({
  getLearningOverview: vi.fn(),
  listLearningProposals: vi.fn(),
  getLearningProposal: vi.fn(),
  listSkills: vi.fn(),
  getSkill: vi.fn(),
  listLearningEvaluations: vi.fn(),
  requireSessionUser: vi.fn(),
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

function fail(error: Partial<ApiError>): ApiResult<never> {
  return { ok: false, error: { kind: "network", path: "/learning", message: "API unreachable.", ...error } };
}

function as(role: Role) {
  api.requireSessionUser.mockResolvedValue(ok(principal(role)));
}

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  nav.notFound.mockClear();
  api.getLearningOverview.mockResolvedValue(ok(learningOverview()));
  api.listLearningProposals.mockResolvedValue(ok(list([proposalSummary()])));
  api.getLearningProposal.mockResolvedValue(ok(proposalDetail()));
  api.listSkills.mockResolvedValue(ok(list([skillSummary()])));
  api.getSkill.mockResolvedValue(ok(skillDetail()));
  api.listLearningEvaluations.mockResolvedValue(ok(list([evaluationSummary()])));
  as("operator_admin");
});

async function renderPage(element: Promise<React.ReactElement>) {
  return render(await element);
}

/** The corpus banner must be on EVERY learning screen (ADR-0016 §16.1). */
function expectCorpusBanner() {
  const banner = screen
    .getAllByRole("alert")
    .find((alert) => alert.textContent?.includes("no proposal here may be promoted") === true);
  expect(banner).toBeDefined();
  expect(banner).toHaveTextContent("65");
  expect(banner).toHaveTextContent("48");
  expect(banner).toHaveTextContent(corpusBanner().note);
  expect(banner).toHaveTextContent("by construction");
  return banner!;
}

describe("the corpus banner is on every learning screen", () => {
  it.each([
    ["/learning", () => LearningPage()],
    ["/learning/proposals", () => ProposalsPage({ searchParams: Promise.resolve({}) })],
    ["/learning/skills", () => SkillsPage({ searchParams: Promise.resolve({}) })],
    ["/learning/evaluations", () => EvaluationsPage({ searchParams: Promise.resolve({}) })],
    ["/learning/proposals/[id]", () => ProposalDetailPage({ params: Promise.resolve({ proposalId: PROPOSAL_ID }) })],
    ["/learning/skills/[name]", () => SkillDetailPage({ params: Promise.resolve({ name: "pick-anchor" }) })],
  ])("%s", async (_path, page) => {
    const view = await renderPage(page());
    expectCorpusBanner();
    view.unmount();
  });

  it("is not dismissible: there is no control to close it", async () => {
    await renderPage(LearningPage());
    const banner = expectCorpusBanner();
    expect(within(banner).queryByRole("button")).toBeNull();
  });
});

describe("/learning", () => {
  it("states the pipeline and that an agent can never promote", async () => {
    await renderPage(LearningPage());
    expect(screen.getByText(/may never promote one/i)).toBeInTheDocument();
    expect(screen.getByText(/never override a deterministic filter/i)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /Proposals by status/ })).toHaveTextContent("Proposed");
    expect(screen.getByRole("table", { name: /Skill versions by status/ })).toHaveTextContent("Promoted");
    expect(screen.getByRole("table", { name: /Evaluation runs by gate result/ })).toHaveTextContent("Inconclusive");
  });

  it("renders a failed overview as a notice instead of inventing a banner", async () => {
    api.getLearningOverview.mockResolvedValue(fail({}));
    await renderPage(LearningPage());
    const alerts = screen.getAllByRole("alert").map((alert) => alert.textContent ?? "");
    expect(alerts.some((text) => text.includes("Learning overview unavailable"))).toBe(true);
    expect(alerts.some((text) => text.includes("no proposal here may be promoted"))).toBe(false);
  });
});

describe("/learning/proposals", () => {
  it("lists proposals and sends only contract filters", async () => {
    await renderPage(ProposalsPage({ searchParams: Promise.resolve({ status: "canary", kind: "bogus" }) }));
    expect(api.listLearningProposals).toHaveBeenCalledWith({ status: "canary", kind: null, limit: 50, offset: 0 });
    expect(screen.getByRole("table", { name: /Learning proposals/ })).toHaveTextContent("Raise the semantic floor");
  });

  it("renders a model-written title as text, never as markup", async () => {
    api.listLearningProposals.mockResolvedValue(
      ok(list([proposalSummary({ title: "<script>window.__pwned = true</script><img src=x onerror=1>" })])),
    );
    const { container } = await renderPage(ProposalsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("table", { name: /Learning proposals/ })).toHaveTextContent("<script>");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
  });
});

describe("/learning/proposals/[proposalId]", () => {
  async function renderDetail(id = PROPOSAL_ID) {
    return renderPage(ProposalDetailPage({ params: Promise.resolve({ proposalId: id }) }));
  }

  it("shows the changes, the gate outcome and the promotion preview", async () => {
    await renderDetail();
    expect(screen.getByRole("table", { name: /Changes to the policy document/ })).toHaveTextContent(
      "ranking.semantic_floor",
    );
    expect(screen.getByRole("table", { name: /Evaluation runs for this proposal/ })).toHaveTextContent("Inconclusive");
    const panel = screen.getByRole("heading", { level: 2, name: "Advance this proposal" }).closest("section")!;
    expect(panel).toHaveTextContent("Only a person may promote");
    // The demonstration-mode refusal is REPORTED, not hidden.
    expect(panel).toHaveTextContent("Promotion refused");
    expect(panel).toHaveTextContent("nothing may be promoted on this data");
    expect(panel).toHaveTextContent("demonstration_mode");
  });

  it("still offers the control, so a mechanical rule does not look like a missing feature", async () => {
    await renderDetail();
    expect(screen.getByRole("button", { name: "Record transition" })).toBeInTheDocument();
    expect(screen.getByLabelText("Move to (required)")).toBeInTheDocument();
    expect(screen.getByLabelText(/Evaluation run id/)).toBeInTheDocument();
  });

  it("mints exactly one operation id per rendered advance form", async () => {
    const { container } = await renderDetail();
    const ids = [...container.querySelectorAll('input[name="operation_id"]')].map((i) => i.getAttribute("value"));
    expect(ids).toHaveLength(1);
    expect(ids[0]).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  it("hides the control from a role without `promote` and names the permission", async () => {
    as("reviewer");
    await renderDetail();
    expect(screen.queryByRole("button", { name: "Record transition" })).toBeNull();
    expect(screen.getByText(/needs the promote permission/i)).toBeInTheDocument();
  });

  // The contract PINS `frozen_fields_untouched` to `true` (a proposal touching a
  // frozen path cannot be constructed), so this state is unreachable through the
  // contract. The console still guards it, because a buggy or newer backend must
  // not be able to slip a frozen-field change past an operator silently. The cast
  // is what makes that defence testable.
  it("warns prominently if a backend ever reports a frozen field was touched", async () => {
    api.getLearningProposal.mockResolvedValue(
      ok({ ...proposalDetail(), frozen_fields_untouched: false } as unknown as ReturnType<typeof proposalDetail>),
    );
    await renderDetail();
    const alerts = screen.getAllByRole("alert").map((alert) => alert.textContent ?? "");
    expect(alerts.some((text) => text.includes("touches a frozen field"))).toBe(true);
  });

  it("404s a malformed id without calling the API", async () => {
    await expect(renderDetail("../evil")).rejects.toThrow("NEXT_NOT_FOUND");
    expect(api.getLearningProposal).not.toHaveBeenCalled();
  });
});

describe("/learning/skills", () => {
  it("shows a version's status, evaluation and who promoted it", async () => {
    await renderPage(SkillsPage({ searchParams: Promise.resolve({ status: "promoted" }) }));
    expect(api.listSkills).toHaveBeenCalledWith({ status: "promoted", limit: 50, offset: 0 });
    const table = screen.getByRole("table", { name: /Skill versions/ });
    expect(table).toHaveTextContent("pick-anchor");
    expect(table).toHaveTextContent("Promoted");
    expect(table).toHaveTextContent("User");
    expect(within(table).getByRole("link", { name: "pick-anchor" })).toHaveAttribute(
      "href",
      "/learning/skills/pick-anchor",
    );
  });

  it("says a version that was never promoted is not in use", async () => {
    api.listSkills.mockResolvedValue(
      ok(list([skillSummary({ status: "draft", promoted_by: null, promoted_at: null, evaluation_score: null })])),
    );
    await renderPage(SkillsPage({ searchParams: Promise.resolve({}) }));
    const table = screen.getByRole("table", { name: /Skill versions/ });
    expect(table).toHaveTextContent("Not promoted");
    expect(table).toHaveTextContent("Not evaluated");
  });
});

describe("/learning/skills/[name]", () => {
  it("renders the skill's own instructions as text, never as markup", async () => {
    api.getSkill.mockResolvedValue(
      ok(skillDetail({ instructions: "<script>window.__pwned2 = true</script>Pick the anchor." })),
    );
    const { container } = await renderPage(SkillDetailPage({ params: Promise.resolve({ name: "pick-anchor" }) }));
    expect(screen.getByRole("heading", { level: 2, name: "Instructions" }).closest("section")).toHaveTextContent(
      "<script>",
    );
    expect(container.querySelector("script")).toBeNull();
    expect((window as unknown as { __pwned2?: boolean }).__pwned2).toBeUndefined();
    expect(screen.getByRole("table", { name: /Every version of pick-anchor/ })).toBeInTheDocument();
  });

  it("404s a skill the API does not have", async () => {
    api.getSkill.mockResolvedValue({
      ok: false,
      error: { kind: "api_error", path: "/learning/skills/{name}", status: 404, code: "not_found", message: "m" },
    });
    await expect(renderPage(SkillDetailPage({ params: Promise.resolve({ name: "nope" }) }))).rejects.toThrow(
      "NEXT_NOT_FOUND",
    );
  });
});

describe("/learning/evaluations", () => {
  it("shows the gate result and never presents inconclusive as a pass", async () => {
    await renderPage(EvaluationsPage({ searchParams: Promise.resolve({ gate_result: "inconclusive" }) }));
    expect(api.listLearningEvaluations).toHaveBeenCalledWith({
      gate_result: "inconclusive",
      limit: 50,
      offset: 0,
    });
    const table = screen.getByRole("table", { name: /Evaluation runs/ });
    expect(table).toHaveTextContent("Inconclusive");
    expect(table).not.toHaveTextContent("Pass");
    expect(screen.getByText(/never treated as a pass/i)).toBeInTheDocument();
  });
});
