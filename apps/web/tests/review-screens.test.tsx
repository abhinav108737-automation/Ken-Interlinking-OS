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

import { list } from "./fixtures/inventory";
import { ANCHOR, PLAN_ID, linkPlan, linkPlanDetail, recommendationSummary } from "./fixtures/recommendations";
import { NEW_PLAN_ID, dashboardSummary, eventRecord, policiesView, principal, reviewAction, targetOption } from "./fixtures/review";

const api = vi.hoisted(() => ({
  getSessionUser: vi.fn(),
  requireSessionUser: vi.fn(),
  getDashboard: vi.fn(),
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
  as("reviewer");
  api.getRecommendationSummary.mockResolvedValue(ok(recommendationSummary()));
  api.listAgentPlanReviews.mockResolvedValue(ok([]));
  api.listPlanReviewActions.mockResolvedValue(ok([]));
  api.listPlanTargetOptions.mockResolvedValue(ok([targetOption()]));
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
    expect(screen.getByRole("note")).toHaveTextContent("no review action applies");
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

describe("/recommendations inbox by role", () => {
  it("reviewer: bulk form and checkboxes for plans awaiting approval", async () => {
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan({ status: "awaiting_approval" })])));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Recommendation inbox");
    expect(screen.getByRole("form", { name: "Bulk review" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: `Select “${ANCHOR}” for bulk review` })).toBeInTheDocument();
  });

  it("viewer: read-only inbox", async () => {
    as("viewer");
    api.listLinkPlans.mockResolvedValue(ok(list([linkPlan({ status: "awaiting_approval" })])));
    await renderPage(RecommendationsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.queryByRole("form", { name: "Bulk review" })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.getByText(/can read recommendations but not review them/)).toBeInTheDocument();
  });
});

describe("dashboard", () => {
  it("shows counts, the awaiting-approval call to action, AUTO_APPROVE disabled and recent reviews", async () => {
    api.getDashboard.mockResolvedValue(ok(dashboardSummary()));
    await renderPage(DashboardPage());
    expect(screen.getByText("recommendations are awaiting approval.").previousElementSibling).toHaveTextContent("7");
    expect(screen.getByRole("link", { name: "Review the inbox" })).toHaveAttribute("href", "/recommendations");
    expect(screen.getByText("AUTO_APPROVE:").parentElement).toHaveTextContent("Disabled");
    expect(screen.getByText("Live pages").nextElementSibling).toHaveTextContent("118");
    expect(screen.getByRole("table", { name: /Latest review actions/ })).toHaveTextContent("Rejected");
    expect(within(screen.getByRole("table", { name: /Plans by status/ })).getByRole("link", { name: "Approved" })).toHaveAttribute(
      "href",
      "/recommendations?status=approved",
    );
    expect(screen.getByText("No batch job has run yet.")).toBeInTheDocument();
  });

  it("viewer gets a view link, and errors render", async () => {
    as("viewer");
    api.getDashboard.mockResolvedValue(fail({ kind: "network", path: "/dashboard" }));
    await renderPage(DashboardPage());
    expect(screen.getByRole("alert")).toHaveTextContent("Dashboard unavailable");
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

  it("operator_admin: the active policies and AUTO_APPROVE disabled", async () => {
    as("operator_admin");
    api.getPolicies.mockResolvedValue(ok(policiesView()));
    await renderPage(PoliciesPage());
    expect(screen.getByRole("heading", { name: "Ranking policy" })).toBeInTheDocument();
    expect(screen.getByText("AUTO_APPROVE:").parentElement).toHaveTextContent("Disabled");
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
