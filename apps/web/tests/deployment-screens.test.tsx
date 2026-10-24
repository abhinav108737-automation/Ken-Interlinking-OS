import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DeploymentDetailPage from "@/app/deployments/[deploymentId]/page";
import DeploymentsPage from "@/app/deployments/page";
import RecommendationDetailPage from "@/app/recommendations/[planId]/page";
import RecommendationsPage from "@/app/recommendations/page";
import type { ApiError, ApiResult, DeploymentStatus, Role } from "@/lib/api-client";

import { linkPerformanceDetail, linkPerformanceView } from "./fixtures/analytics";
import { DEPLOYMENT_ID, deploymentDetail, deploymentView, twoPlanDeployment } from "./fixtures/deployments";
import { PAGE_ID, list } from "./fixtures/inventory";
import { ANCHOR, OLD_PLAN_ID, PLAN_ID, linkPlan, linkPlanDetail, recommendationSummary } from "./fixtures/recommendations";
import { principal } from "./fixtures/review";

const api = vi.hoisted(() => ({
  // Phase 7: the plan detail screen's performance panel.
  getLinkPerformance: vi.fn(),
  // Phase 7: the deployment detail screen's performance table.
  listLinkPerformance: vi.fn(),
  requireSessionUser: vi.fn(),
  listDeployments: vi.fn(),
  getDeployment: vi.fn(),
  listPlanDeployments: vi.fn(),
  getLinkPlan: vi.fn(),
  listAgentPlanReviews: vi.fn(),
  listPlanReviewActions: vi.fn(),
  listPlanTargetOptions: vi.fn(),
  listLinkPlans: vi.fn(),
  getRecommendationSummary: vi.fn(),
}));
const nav = vi.hoisted(() => ({
  notFound: vi.fn(() => {
    throw new Error("NEXT_NOT_FOUND");
  }),
}));

vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/api")>()), ...api }));
vi.mock("next/navigation", async (importOriginal) => ({
  ...(await importOriginal<typeof import("next/navigation")>()),
  notFound: nav.notFound,
  usePathname: () => "/deployments",
}));

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, status: 200 };
}

function fail(error: Partial<ApiError>): ApiResult<never> {
  return { ok: false, error: { kind: "api_error", path: "/deployments", message: "It failed.", ...error } };
}

function as(role: Role) {
  api.requireSessionUser.mockResolvedValue(ok(principal(role)));
}

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  api.getLinkPerformance.mockResolvedValue({ ok: true, data: linkPerformanceDetail(), status: 200 });
  api.listLinkPerformance.mockResolvedValue({ ok: true, data: list([linkPerformanceView()]), status: 200 });
  nav.notFound.mockClear();
  as("operator_admin");
  api.listPlanDeployments.mockResolvedValue(ok([]));
  api.listAgentPlanReviews.mockResolvedValue(ok([]));
  api.listPlanReviewActions.mockResolvedValue(ok([]));
  api.listPlanTargetOptions.mockResolvedValue(ok([]));
  api.getRecommendationSummary.mockResolvedValue(ok(recommendationSummary()));
});

const XSS_NOTE = '<img src=x onerror="alert(1)">';

describe("/deployments history", () => {
  it("lists deployments with status, source, plan count and a detail link", async () => {
    api.listDeployments.mockResolvedValue(ok(list([deploymentView(), deploymentView({ id: OLD_PLAN_ID, status: "conflict", error_code: "cms_version_conflict", new_version: null })])));
    render(await DeploymentsPage({ searchParams: Promise.resolve({}) }));
    expect(api.listDeployments).toHaveBeenCalledWith({ status: null, source_page_id: null, limit: 50, offset: 0 });
    const table = screen.getByRole("table", { name: /Deployment history/ });
    expect(table).toHaveTextContent("Verified");
    expect(table).toHaveTextContent("Version conflict");
    expect(table).toHaveTextContent("cms_version_conflict");
    expect(within(table).getAllByRole("link", { name: /view deployment/ })[0]).toHaveAttribute("href", `/deployments/${DEPLOYMENT_ID}`);
    expect(table).toHaveTextContent("olivia");
  });

  it("tags an autonomous deployment with an Autonomous badge and not a human one", async () => {
    const autonomous = deploymentView({
      id: OLD_PLAN_ID,
      autonomous: true,
      autonomy_decision_id: 41,
      autonomy_decision: "auto_publish_with_extra_verification",
      autonomy_mode: "autonomous",
    });
    api.listDeployments.mockResolvedValue(ok(list([deploymentView(), autonomous])));
    render(await DeploymentsPage({ searchParams: Promise.resolve({}) }));
    const rows = within(screen.getByRole("table", { name: /Deployment history/ })).getAllByRole("row");
    expect(within(rows[1]!).queryByText("Autonomous")).toBeNull();
    expect(within(rows[2]!).getByText("Autonomous")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("Autonomous").parentElement).toHaveAttribute("title", expect.stringContaining("decision #41"));
  });

  it("applies valid filters and paging; ignores invalid ones", async () => {
    api.listDeployments.mockResolvedValue(ok(list([deploymentView()], { total: 120, offset: 50, has_more: true })));
    render(await DeploymentsPage({ searchParams: Promise.resolve({ status: "rolled_back", source_page_id: PAGE_ID, offset: "50" }) }));
    expect(api.listDeployments).toHaveBeenCalledWith({ status: "rolled_back", source_page_id: PAGE_ID, limit: 50, offset: 50 });
    expect(screen.getByLabelText("Status")).toHaveValue("rolled_back");
    expect(screen.getByRole("link", { name: "Next →" })).toHaveAttribute("href", `/deployments?status=rolled_back&source_page_id=${PAGE_ID}&offset=100`);
    render(await DeploymentsPage({ searchParams: Promise.resolve({ status: "evil", source_page_id: "../x" }) }));
    expect(api.listDeployments).toHaveBeenLastCalledWith({ status: null, source_page_id: null, limit: 50, offset: 0 });
    expect(screen.getByText(/source page id filter was ignored/)).toBeInTheDocument();
  });

  it("empty and error states", async () => {
    api.listDeployments.mockResolvedValue(ok(list([])));
    const view = render(await DeploymentsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("status")).toHaveTextContent("No deployments yet.");
    view.unmount();
    api.listDeployments.mockResolvedValue(fail({ kind: "network" }));
    render(await DeploymentsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("Deployments unavailable");
  });
});

async function renderDetail(search: Record<string, string> = {}) {
  return render(
    await DeploymentDetailPage({ params: Promise.resolve({ deploymentId: DEPLOYMENT_ID }), searchParams: Promise.resolve(search) }),
  );
}

describe("/deployments/[deploymentId]", () => {
  it("shows the autonomous badge, decision id and decision on an autonomous deployment only", async () => {
    api.getDeployment.mockResolvedValue(
      ok(deploymentDetail({ autonomous: true, autonomy_decision_id: 41, autonomy_decision: "auto_publish", autonomy_mode: "autonomous" })),
    );
    const view = await renderDetail();
    expect(screen.getByText("Autonomous")).toBeInTheDocument();
    const facts = screen.getByRole("heading", { level: 2, name: "Deployment" }).closest("section")!;
    expect(facts).toHaveTextContent("Autonomous publish");
    expect(facts).toHaveTextContent("decision #41");
    expect(facts).toHaveTextContent("auto_publish");
    view.unmount();
    api.getDeployment.mockResolvedValue(ok(deploymentDetail()));
    await renderDetail();
    expect(screen.queryByText("Autonomous")).toBeNull();
    expect(screen.queryByText("Autonomous publish")).toBeNull();
  });

  it("shows the header facts, items, verification checks and preview as text", async () => {
    api.getDeployment.mockResolvedValue(ok(deploymentDetail()));
    const { container } = await renderDetail({ deployed: "1" });
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Deployment to https://www.example.com/reports/widgets");
    expect(screen.getByText(/Your deployment request was recorded/)).toBeInTheDocument();
    const facts = screen.getByRole("heading", { level: 2, name: "Deployment" }).closest("section")!;
    for (const text of ["simulated", "cms-42", "v7", "v8", "olivia"]) expect(facts).toHaveTextContent(text);
    const items = screen.getByRole("table", { name: /Recommendations in this deployment/ });
    expect(within(items).getByRole("link", { name: /lithium-ion battery market/ })).toHaveAttribute("href", `/recommendations/${PLAN_ID}`);
    const checks = screen.getByRole("table", { name: /Checks of verification run 1/ });
    expect(checks).toHaveTextContent("Link present exactly once");
    expect(checks).toHaveTextContent("Passed");
    expect(checks).toHaveTextContent("Inconclusive");
    expect(checks).toHaveTextContent("http_timeout");
    // Preview: the HTML snippets are literal text in <pre>; nothing is rendered.
    const pre = container.querySelectorAll("pre");
    expect(pre[0]).toHaveTextContent("<script>window.__pwned = true</script>");
    expect(pre[1]).toHaveTextContent('<img src=x onerror="alert(1)">');
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("article mark")).toHaveTextContent(ANCHOR);
  });

  it.each(["viewer", "reviewer"] as const)("%s: no verify or rollback controls", async (role) => {
    as(role);
    api.getDeployment.mockResolvedValue(ok(deploymentDetail()));
    await renderDetail();
    expect(screen.queryByRole("form")).toBeNull();
    expect(screen.queryByRole("button", { name: /verify|roll back/i })).toBeNull();
    expect(screen.getByText(/can read deployments but not verify or roll them back/)).toBeInTheDocument();
  });

  it.each(["applied", "verified", "verification_failed"] as const)("operator: %s offers re-verify and rollback", async (status) => {
    api.getDeployment.mockResolvedValue(ok(deploymentDetail({ status })));
    await renderDetail();
    expect(screen.getByRole("form", { name: "Re-verify" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Roll back" })).toBeInTheDocument();
  });

  it.each(["failed", "conflict", "rolled_back", "applying", "rolling_back"] as DeploymentStatus[])(
    "operator: %s hides the actions",
    async (status) => {
      api.getDeployment.mockResolvedValue(ok(deploymentDetail({ status })));
      await renderDetail();
      expect(screen.queryByRole("form")).toBeNull();
      expect(screen.queryByRole("button", { name: /verify|roll back/i })).toBeNull();
    },
  );

  it.each(["applying", "rolling_back"] as const)("%s explains reconciliation", async (status) => {
    api.getDeployment.mockResolvedValue(ok(deploymentDetail({ status, verifications: [] })));
    await renderDetail();
    const notice = screen.getByText(/the outcome is not known yet/).closest("div")!;
    expect(notice).toHaveTextContent("make deployments-reconcile");
    expect(notice).toHaveTextContent("never guessed");
    expect(screen.getByText("No verification has run yet.")).toBeInTheDocument();
  });

  it("shows the rollback reason and note as text", async () => {
    api.getDeployment.mockResolvedValue(ok(deploymentDetail({ status: "rolled_back", rollback_reason: "other", rollback_note: XSS_NOTE, rolled_back_at: "2026-10-15T09:00:00Z" })));
    const { container } = await renderDetail();
    const section = screen.getByRole("heading", { level: 2, name: "Rollback" }).closest("section")!;
    expect(section).toHaveTextContent("Other (explain in the note)");
    expect(section).toHaveTextContent(XSS_NOTE);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByRole("note")).toHaveTextContent("nothing of it is on the page");
  });

  it("offers per-link rollback only for links on the page", async () => {
    api.getDeployment.mockResolvedValue(ok(twoPlanDeployment()));
    await renderDetail();
    const rollback = screen.getByRole("form", { name: "Roll back" });
    expect(within(rollback).getAllByRole("checkbox")).toHaveLength(2);
    expect(within(rollback).getByLabelText("“battery recycling”")).toBeInTheDocument();
  });

  it("not found and error states", async () => {
    api.getDeployment.mockResolvedValue(fail({ status: 404, code: "not_found" }));
    await expect(renderDetail()).rejects.toThrow("NEXT_NOT_FOUND");
    api.getDeployment.mockResolvedValue(fail({ kind: "network" }));
    await renderDetail();
    expect(screen.getByRole("alert")).toHaveTextContent("Deployment unavailable");
  });
});

describe("plan detail: deployment panel", () => {
  async function renderPlan() {
    return render(await RecommendationDetailPage({ params: Promise.resolve({ planId: PLAN_ID }), searchParams: Promise.resolve({}) }));
  }

  it("operator: an approved plan offers the dry run; its deployments are listed", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "approved" })));
    api.listPlanDeployments.mockResolvedValue(ok([deploymentView({ status: "rolled_back" })]));
    const { container } = await renderPlan();
    expect(api.listPlanDeployments).toHaveBeenCalledWith(PLAN_ID);
    const deployForm = screen.getByRole("form", { name: "Deploy" });
    expect(within(deployForm).getByRole("button", { name: "Dry run" })).toBeInTheDocument();
    expect(deployForm.querySelector<HTMLInputElement>('input[type="hidden"][name="plan_ids"]')?.value).toBe(PLAN_ID);
    const table = screen.getByRole("table", { name: /Deployments that included this recommendation/ });
    expect(table).toHaveTextContent("Rolled back");
    expect(container.querySelectorAll("input[form]")).toHaveLength(0);
  });

  it.each(["viewer", "reviewer"] as const)("%s: no deploy control, a role note", async (role) => {
    as(role);
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "approved" })));
    await renderPlan();
    expect(screen.queryByRole("form", { name: "Deploy" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Dry run" })).toBeNull();
    expect(screen.getByText(/cannot deploy it: an operator admin deploys/)).toBeInTheDocument();
  });

  it("a plan that is not approved cannot be deployed", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "verified" })));
    api.listPlanDeployments.mockResolvedValue(fail({ kind: "network", path: "/recommendations/{plan_id}/deployments" }));
    await renderPlan();
    expect(screen.queryByRole("form", { name: "Deploy" })).toBeNull();
    expect(screen.getByText(/Only approved recommendations can be deployed; this one is verified/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Deployments unavailable");
  });
});

describe("inbox: multi-select deploy", () => {
  it("operator on status=approved: deployment checkboxes bound to the dry-run form, with their source page", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan({ status: "approved" }), linkPlan({ id: OLD_PLAN_ID, status: "approved", anchor_text: "battery recycling" })])));
    await render(await RecommendationsPage({ searchParams: Promise.resolve({ status: "approved" }) }));
    expect(screen.getByRole("form", { name: "Deploy" })).toHaveAttribute("id", "deploy-dry-run-form");
    const box = screen.getByRole("checkbox", { name: `Select “${ANCHOR}” for deployment` });
    expect(box).toHaveAttribute("form", "deploy-dry-run-form");
    expect(box).toHaveAttribute("data-source-page-id", PAGE_ID);
    expect(screen.queryByRole("form", { name: "Bulk review" })).toBeNull();
  });

  it("no deploy selection on other filters or for reviewers", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan({ status: "approved" })])));
    const view = render(await RecommendationsPage({ searchParams: Promise.resolve({ status: "all" }) }));
    expect(screen.queryByRole("form", { name: "Deploy" })).toBeNull();
    view.unmount();
    as("reviewer");
    render(await RecommendationsPage({ searchParams: Promise.resolve({ status: "approved" }) }));
    expect(screen.queryByRole("form", { name: "Deploy" })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});
