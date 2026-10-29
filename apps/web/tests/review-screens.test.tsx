import { render, screen, within } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import RootLayout from "@/app/layout";
import DashboardPage from "@/app/page";
import AuditEventsPage from "@/app/audit/events/page";
import AuditReviewsPage from "@/app/audit/reviews/page";
import LoginPage from "@/app/login/page";
import RecommendationDetailPage from "@/app/recommendations/[planId]/page";
import RecommendationsPage from "@/app/recommendations/page";
import PoliciesPage from "@/app/settings/policies/page";
import type { ApiError, ApiResult, Role } from "@/lib/api-client";

import { linkPerformanceDetail } from "./fixtures/analytics";
import { list } from "./fixtures/inventory";
import { ANCHOR, PLAN_ID, linkPlan, linkPlanDetail, recommendationSummary } from "./fixtures/recommendations";
import { NEW_PLAN_ID, dashboardSummary, eventRecord, policiesView, principal, reviewAction, targetOption } from "./fixtures/review";
import { autonomyHealth, autonomySummary } from "./fixtures/autonomy";

const api = vi.hoisted(() => ({
  // Phase 7: the plan detail screen's performance panel.
  getLinkPerformance: vi.fn(),
  // Phase 6: the plan detail screen's deployment panel.
  listPlanDeployments: vi.fn(),
  getSessionUser: vi.fn(),
  requireSessionUser: vi.fn(),
  getDashboard: vi.fn(),
  // ADR-0018: the dashboard, the layout banner and the recommendations list
  // all read the autonomy mode.
  getAutonomyMode: vi.fn(),
  getAutonomySummary: vi.fn(),
  listLinkPlans: vi.fn(),
  getRecommendationSummary: vi.fn(),
  getLinkPlan: vi.fn(),
  listAgentPlanReviews: vi.fn(),
  listPlanReviewActions: vi.fn(),
  listPlanTargetOptions: vi.fn(),
  listReviewActions: vi.fn(),
  listEvents: vi.fn(),
  getPolicies: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

vi.mock("next/navigation", async (importOriginal) => ({
  ...(await importOriginal<typeof import("next/navigation")>()),
  usePathname: () => "/",
}));

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, status: 200 };
}

function fail(error: Partial<ApiError>): ApiResult<never> {
  return { ok: false, error: { kind: "api_error", path: "/auth/me", message: "No session.", ...error } };
}

function as(role: Role) {
  api.getSessionUser.mockResolvedValue(ok(principal(role)));
  api.requireSessionUser.mockResolvedValue(ok(principal(role)));
}

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  api.getLinkPerformance.mockResolvedValue({ ok: true, data: linkPerformanceDetail(), status: 200 });
  api.listPlanDeployments.mockResolvedValue({ ok: true, data: [], status: 200 });
  as("reviewer");
  api.getRecommendationSummary.mockResolvedValue(ok(recommendationSummary()));
  api.listAgentPlanReviews.mockResolvedValue(ok([]));
  api.listPlanReviewActions.mockResolvedValue(ok([]));
  api.listPlanTargetOptions.mockResolvedValue(ok([targetOption()]));
  api.getAutonomyMode.mockResolvedValue(ok(autonomyHealth()));
  api.getAutonomySummary.mockResolvedValue(ok(autonomySummary()));
});

async function renderPage(element: Promise<React.ReactElement>) {
  return render(await element);
}

describe("/recommendations/[planId] by role", () => {
  async function renderDetail(search: Record<string, string> = {}) {
    return renderPage(
      RecommendationDetailPage({ params: Promise.resolve({ planId: PLAN_ID }), searchParams: Promise.resolve(search) }),
    );
  }

  it("viewer: no review controls, a role note, no target-options call", async () => {
    as("viewer");
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "awaiting_approval" })));
    await renderDetail();
    expect(screen.queryAllByRole("form")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /approve|reject|save|replace/i })).toBeNull();
    expect(screen.getByText(/Your role \(Viewer\) can read recommendations but not review them/)).toBeInTheDocument();
    expect(api.listPlanTargetOptions).not.toHaveBeenCalled();
  });

  it.each(["reviewer", "operator_admin"] as const)("%s: all four actions for a plan awaiting approval", async (role) => {
    as(role);
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "awaiting_approval" })));
    await renderDetail();
    for (const name of ["Approve", "Reject", "Edit the anchor or placement", "Replace the destination"]) {
      expect(screen.getByRole("form", { name })).toBeInTheDocument();
    }
    expect(api.listPlanTargetOptions).toHaveBeenCalledWith(PLAN_ID);
    const preview = screen.getByRole("heading", { level: 2, name: "Placement preview" }).closest("section")!;
    expect(within(preview).getByText("After (with the proposed link)").closest("figure")!.querySelector("mark")).toHaveTextContent(ANCHOR);
  });

  it("shows the human review history with reasons and notes as text", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "rejected" })));
    api.listPlanReviewActions.mockResolvedValue(
      ok([reviewAction({ note: "<b>not bold</b>", reasons: ["over_linking"] })]),
    );
    const { container } = await renderDetail();
    const history = screen.getByRole("table", { name: /Human review actions on this recommendation/ });
    expect(history).toHaveTextContent("Rejected");
    expect(history).toHaveTextContent("Over-linking");
    expect(history).toHaveTextContent("<b>not bold</b>");
    expect(container.querySelector("b")).toBeNull();
    expect(
      screen.getAllByRole("note").some((note) => note.textContent?.includes("no review action applies") === true),
    ).toBe(true);
    expect(api.listPlanTargetOptions).not.toHaveBeenCalled();
  });

  it("confirms an edit only when the data shows an edited, superseding plan", async () => {
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ id: NEW_PLAN_ID, anchor_source: "edited", supersedes: PLAN_ID })));
    await renderDetail({ edited: "1" });
    expect(screen.getByText(/Your edit created this recommendation/)).toBeInTheDocument();
    screen.getByText(/Your edit created/).remove();
    api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ anchor_source: "target_topic" })));
    await renderDetail({ edited: "1" });
    expect(screen.queryByText(/Your edit created this recommendation/)).toBeNull();
  });
});

// ADR-0018 §5: the screen is a monitoring list. Bulk review survives only as an
// operator EXCEPTION action and must be labelled as one.
describe("/recommendations by role", () => {
  it("reviewer: bulk review survives as an exception action, clearly marked", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan({ status: "awaiting_approval" })])));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Recommendations");
    expect(screen.getByRole("heading", { level: 1 })).not.toHaveTextContent("inbox");
    expect(screen.getByText(/Approve, reject and edit are operator exception actions/)).toBeInTheDocument();
    expect(screen.getByText(/not part of the normal workflow/)).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Bulk review" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: `Select “${ANCHOR}” for bulk review` })).toBeInTheDocument();
  });

  it("viewer: no exception controls, and the missing permission is named", async () => {
    as("viewer");
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan({ status: "awaiting_approval" })])));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.queryByRole("form", { name: "Bulk review" })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.getByText(/needs the review permission, which your role does not have/)).toBeInTheDocument();
  });

  it("the autonomy mode is on the screen, and shadow says nothing is published", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan()])));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    const banner = screen.getByRole("note", { name: "Autonomy mode" });
    expect(banner).toHaveTextContent("Shadow — nothing is published");
    expect(banner).toHaveTextContent("Publishing disabled");
  });
});

// ADR-0018 §5: the dashboard is organised around the operational states, not
// around an approval queue. Approval appears once, as an exception.
describe("dashboard", () => {
  it("leads with the autonomy mode and the operational states, not an approval queue", async () => {
    api.getDashboard.mockResolvedValue(ok(dashboardSummary()));
    await renderPage(DashboardPage());
    // The mode, and in shadow the fact that nothing is published.
    const banners = screen.getAllByRole("note", { name: "Autonomy mode" });
    expect(banners[0]).toHaveTextContent("Shadow — nothing is published");
    expect(banners.some((banner) => banner.textContent?.includes("counterfactual") === true)).toBe(true);
    // The seven primary operational states, each with its own labelled row.
    const states = screen.getByRole("table", { name: /Link plans by operational state/ });
    for (const label of [
      "Proposed",
      "Blocked",
      "Auto-published",
      "Verification pending",
      "Verified",
      "Rolled back",
      "Failed",
    ]) {
      expect(states).toHaveTextContent(label);
    }
    // Blocked plans are an investigation call to action, not an approval one.
    expect(screen.getByRole("link", { name: "Investigate blocked actions" })).toHaveAttribute(
      "href",
      "/autonomy/blocked",
    );
    expect(screen.queryByRole("link", { name: "Review the inbox" })).toBeNull();
    // Approval survives only under "Operator exceptions".
    const exceptions = screen.getByRole("heading", { level: 2, name: "Operator exceptions" }).closest("section")!;
    expect(exceptions).toHaveTextContent("Approval is not part of the normal workflow");
    expect(within(exceptions).getByRole("link", { name: "Handle exceptions" })).toHaveAttribute(
      "href",
      "/recommendations?status=awaiting_approval",
    );
    expect(within(exceptions).getByText("plans are deferred to an operator.").previousElementSibling).toHaveTextContent(
      "7",
    );
    // Autonomy counts, and the content counts, still render.
    expect(screen.getByRole("link", { name: "4" })).toHaveAttribute("href", "/autonomy/attempts");
    expect(screen.getByText("Live pages").nextElementSibling).toHaveTextContent("118");
    expect(screen.getByRole("table", { name: /Latest operator actions on exceptions/ })).toHaveTextContent("Rejected");
    expect(screen.getByText("No batch job has run yet.")).toBeInTheDocument();
  });

  it("a shadow decision count is labelled as a counterfactual", async () => {
    api.getDashboard.mockResolvedValue(ok(dashboardSummary()));
    await renderPage(DashboardPage());
    const byMode = screen.getByRole("table", { name: /Decisions by mode/ });
    expect(byMode).toHaveTextContent("Shadow (counterfactual — nothing published)");
  });

  it("viewer gets a view link, and errors render", async () => {
    as("viewer");
    api.getDashboard.mockResolvedValue(fail({ kind: "network", path: "/dashboard" }));
    await renderPage(DashboardPage());
    const alerts = screen.getAllByRole("alert");
    expect(alerts.some((alert) => alert.textContent?.includes("Dashboard unavailable") === true)).toBe(true);
  });

  it("renders with whatever came back when the autonomy calls fail", async () => {
    api.getDashboard.mockResolvedValue(ok(dashboardSummary()));
    api.getAutonomySummary.mockResolvedValue(fail({ kind: "network", path: "/autonomy" }));
    api.getAutonomyMode.mockResolvedValue(fail({ kind: "network", path: "/autonomy/mode" }));
    await renderPage(DashboardPage());
    const alerts = screen.getAllByRole("alert");
    expect(alerts.some((alert) => alert.textContent?.includes("Autonomy summary unavailable") === true)).toBe(true);
    // The operational states still render: one failed call never blanks the page.
    expect(screen.getByRole("table", { name: /Link plans by operational state/ })).toBeInTheDocument();
    // And no mode banner is invented when the mode is unknown.
    expect(screen.queryByRole("note", { name: "Autonomy mode" })).toBeNull();
  });
});

describe("audit and settings by role", () => {
  it.each(["viewer", "reviewer"] as const)("%s cannot open the audit log or settings (no API call)", async (role) => {
    as(role);
    for (const [page, permission] of [
      [AuditReviewsPage({ searchParams: Promise.resolve({}) }), "Read the audit log"],
      [AuditEventsPage({ searchParams: Promise.resolve({}) }), "Read the audit log"],
      [PoliciesPage(), "Read settings and policies"],
    ] as const) {
      const view = await renderPage(page);
      expect(screen.getByRole("alert")).toHaveTextContent("Not available for your role");
      expect(screen.getByRole("alert")).toHaveTextContent(permission);
      view.unmount();
    }
    expect(api.listReviewActions).not.toHaveBeenCalled();
    expect(api.listEvents).not.toHaveBeenCalled();
    expect(api.getPolicies).not.toHaveBeenCalled();
  });

  it("operator_admin: review actions with filters", async () => {
    as("operator_admin");
    api.listReviewActions.mockResolvedValue(ok(list([reviewAction({ note: "<script>x</script>" })], { total: 1 })));
    const { container } = await renderPage(
      AuditReviewsPage({ searchParams: Promise.resolve({ action: "reject", actor: "user:jane", offset: "50" }) }),
    );
    expect(api.listReviewActions).toHaveBeenCalledWith({ action: "reject", actor: "user:jane", limit: 50, offset: 50 });
    expect(screen.getByRole("table", { name: /Review actions/ })).toHaveTextContent("<script>x</script>");
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByLabelText("Action")).toHaveValue("reject");
  });

  it("operator_admin: events with an event-name filter; invalid filters are not sent", async () => {
    as("operator_admin");
    api.listEvents.mockResolvedValue(ok(list([eventRecord()])));
    await renderPage(AuditEventsPage({ searchParams: Promise.resolve({ event_name: "link.approved" }) }));
    expect(api.listEvents).toHaveBeenCalledWith({ event_name: "link.approved", subject_id: null, limit: 50, offset: 0 });
    expect(screen.getByRole("table", { name: /Events/ })).toHaveTextContent('"actor":"user:jane"');
    await renderPage(AuditEventsPage({ searchParams: Promise.resolve({ event_name: "evil" }) }));
    expect(api.listEvents).toHaveBeenLastCalledWith(expect.objectContaining({ event_name: null }));
  });

  // ADR-0018 §2 RETIRED the AUTO_APPROVE flag rather than reusing it, and the
  // policy screen has to say so rather than presenting it as the publishing
  // switch it no longer is.
  it("operator_admin: the active policies, the retired flag and the autonomy mode", async () => {
    as("operator_admin");
    api.getPolicies.mockResolvedValue(ok(policiesView()));
    await renderPage(PoliciesPage());
    expect(screen.getByRole("heading", { name: "Ranking policy" })).toBeInTheDocument();
    const flag = screen.getByText("AUTO_APPROVE (retired flag):").parentElement!;
    expect(flag).toHaveTextContent("Disabled");
    expect(flag).toHaveTextContent("the flag is retired");
    expect(flag).toHaveTextContent("governed by the autonomy mode");
    // The screen names what DOES govern publishing, and the mode is on it.
    expect(screen.getByRole("note", { name: "Autonomy mode" })).toHaveTextContent("Shadow");
    expect(screen.getByRole("link", { name: "autonomy mode" })).toHaveAttribute("href", "/autonomy");
    expect(screen.getByRole("link", { name: "learning" })).toHaveAttribute("href", "/learning");
    expect(screen.getByRole("link", { name: "system status" })).toHaveAttribute("href", "/system");
  });

  it("a 403 from the API still renders a notice", async () => {
    as("operator_admin");
    api.listReviewActions.mockResolvedValue(fail({ status: 403, code: "permission_denied", path: "/review/actions" }));
    await renderPage(AuditReviewsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("Review actions unavailable");
  });
});

describe("layout header and navigation", () => {
  async function markup(): Promise<string> {
    return renderToStaticMarkup(await RootLayout({ children: <p>child</p> }));
  }

  it.each([
    ["viewer", ["Dashboard", "Recommendations", "System"], ["Audit", "Settings"]],
    ["reviewer", ["Dashboard", "Recommendations"], ["Audit", "Settings"]],
    ["operator_admin", ["Audit", "Settings", "System"], []],
  ] as const)("%s sees the right sections, their name, role and sign-out", async (role, shown, hidden) => {
    as(role);
    const html = await markup();
    for (const label of shown) expect(html).toContain(`>${label}</a>`);
    for (const label of hidden) expect(html).not.toContain(`>${label}</a>`);
    expect(html).toContain("Jane Reviewer");
    expect(html).toContain("Sign out");
  });

  it("signed out: a sign-in link and no navigation", async () => {
    api.getSessionUser.mockResolvedValue(fail({ status: 401, code: "unauthenticated" }));
    const html = await markup();
    expect(html).toContain('href="/login"');
    expect(html).not.toContain('aria-label="Primary"');
  });

  it("API unreachable: navigation stays (System status helps)", async () => {
    api.getSessionUser.mockResolvedValue(fail({ kind: "network" }));
    expect(await markup()).toContain('aria-label="Primary"');
  });
});

describe("/login", () => {
  it("carries a safe next and explains an ended session", async () => {
    const { container } = await renderPage(LoginPage({ searchParams: Promise.resolve({ next: "//evil.example", reason: "expired" }) }));
    expect(container.querySelector<HTMLInputElement>('input[name="next"]')?.value).toBe("/");
    expect(screen.getByText(/Your session has ended/)).toBeInTheDocument();
    await renderPage(LoginPage({ searchParams: Promise.resolve({ next: "/audit/reviews" }) }));
    expect(container.ownerDocument.querySelectorAll<HTMLInputElement>('input[name="next"]')[1]?.value).toBe("/audit/reviews");
  });
});
