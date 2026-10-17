import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import BlockedActionDetailPage from "@/app/autonomy/blocked/[planId]/page";
import BlockedActionsPage from "@/app/autonomy/blocked/page";
import PublishAttemptsPage from "@/app/autonomy/attempts/page";
import AutonomyDecisionsPage from "@/app/autonomy/decisions/page";
import AutonomyPage from "@/app/autonomy/page";
import SystemPage from "@/app/system/page";
import type { ApiError, ApiResult, Role } from "@/lib/api-client";

import {
  autonomyDecision,
  autonomyHealth,
  autonomySummary,
  blockedAction,
  publishAttempt,
} from "./fixtures/autonomy";
import { list } from "./fixtures/inventory";
import { PLAN_ID, linkPlanDetail, recommendationSummary } from "./fixtures/recommendations";
import { principal } from "./fixtures/review";

const api = vi.hoisted(() => ({
  getAutonomyMode: vi.fn(),
  getAutonomySummary: vi.fn(),
  listAutonomyDecisions: vi.fn(),
  listBlockedActions: vi.fn(),
  listPublishAttempts: vi.fn(),
  getRecommendationSummary: vi.fn(),
  getLinkPlan: vi.fn(),
  requireSessionUser: vi.fn(),
  getReady: vi.fn(),
  getMeta: vi.fn(),
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
  return { ok: false, error: { kind: "network", path: "/autonomy", message: "API unreachable.", ...error } };
}

function as(role: Role) {
  api.requireSessionUser.mockResolvedValue(ok(principal(role)));
}

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  nav.notFound.mockClear();
  api.getAutonomyMode.mockResolvedValue(ok(autonomyHealth()));
  api.getAutonomySummary.mockResolvedValue(ok(autonomySummary()));
  api.listAutonomyDecisions.mockResolvedValue(ok(list([autonomyDecision()])));
  api.listBlockedActions.mockResolvedValue(ok(list([blockedAction()])));
  api.listPublishAttempts.mockResolvedValue(ok(list([publishAttempt()])));
  api.getRecommendationSummary.mockResolvedValue(ok(recommendationSummary()));
  api.getLinkPlan.mockResolvedValue(ok(linkPlanDetail({ status: "blocked" })));
  as("operator_admin");
});

async function renderPage(element: Promise<React.ReactElement>) {
  return render(await element);
}

describe("/autonomy", () => {
  it("leads with the mode, the operational states and the decision and block counts", async () => {
    await renderPage(AutonomyPage());
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Autonomy");
    const banner = screen.getAllByRole("note", { name: "Autonomy mode" })[0]!;
    expect(banner).toHaveTextContent("Shadow — nothing is published");
    expect(banner).toHaveTextContent("Publishing disabled");
    // The CMS and calibration facts an operator needs to trust the mode.
    expect(banner).toHaveTextContent("filesystem");
    expect(banner).toHaveTextContent("none — confidence is raw");
    expect(screen.getByRole("table", { name: /Link plans by operational state/ })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /Deterministic blocks by code/ })).toHaveTextContent("Protected page");
    expect(screen.getByRole("link", { name: "blocked actions" })).toHaveAttribute("href", "/autonomy/blocked");
  });

  it("says in the page text that there is no approval step", async () => {
    await renderPage(AutonomyPage());
    expect(screen.getByText(/no approval step in the normal workflow/i)).toBeInTheDocument();
  });

  it("renders every failed call as a notice without blanking the page", async () => {
    api.getAutonomySummary.mockResolvedValue(fail({ path: "/autonomy" }));
    api.listAutonomyDecisions.mockResolvedValue(fail({ path: "/autonomy/decisions" }));
    await renderPage(AutonomyPage());
    const alerts = screen.getAllByRole("alert").map((alert) => alert.textContent ?? "");
    expect(alerts.some((text) => text.includes("Autonomy summary unavailable"))).toBe(true);
    expect(alerts.some((text) => text.includes("Autonomy decisions unavailable"))).toBe(true);
    expect(screen.getByRole("table", { name: /Link plans by operational state/ })).toBeInTheDocument();
  });
});

describe("/autonomy/decisions", () => {
  it("labels a shadow row as a counterfactual and names what would have happened", async () => {
    await renderPage(AutonomyDecisionsPage({ searchParams: Promise.resolve({}) }));
    const row = within(screen.getByRole("table", { name: /Autonomy decisions/ })).getAllByRole("row")[1]!;
    expect(row).toHaveTextContent("Shadow — did not happen");
    expect(row).toHaveTextContent("Would have been: Auto-publish.");
    expect(row).toHaveTextContent("Block");
    expect(row).toHaveTextContent("Medium risk");
    // Raw confidence is never presented as calibrated.
    expect(row).toHaveTextContent("0.630 (raw)");
    expect(row).not.toHaveTextContent("calibrated");
  });

  it("does not call a shadow row 'did not happen' when the mode was autonomous", async () => {
    api.listAutonomyDecisions.mockResolvedValue(
      ok(list([autonomyDecision({ mode: "autonomous", would_have_decision: null, decision: "auto_publish" })])),
    );
    await renderPage(AutonomyDecisionsPage({ searchParams: Promise.resolve({}) }));
    const row = within(screen.getByRole("table", { name: /Autonomy decisions/ })).getAllByRole("row")[1]!;
    expect(row).not.toHaveTextContent("did not happen");
    expect(row).toHaveTextContent("Auto-publish");
  });

  it("sends only contract filters and ignores an invalid plan id", async () => {
    await renderPage(
      AutonomyDecisionsPage({
        searchParams: Promise.resolve({ decision: "block", mode: "yolo", plan_id: "../x" }),
      }),
    );
    expect(api.listAutonomyDecisions).toHaveBeenCalledWith({
      decision: "block",
      mode: null,
      plan_id: null,
      limit: 50,
      offset: 0,
    });
    expect(screen.getByRole("alert")).toHaveTextContent("must be a UUID");
  });
});

describe("/autonomy/blocked", () => {
  it("lists the codes that fired and offers an investigation route, not an approval", async () => {
    await renderPage(BlockedActionsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Blocked actions");
    const table = screen.getByRole("table", { name: /Blocked actions/ });
    expect(table).toHaveTextContent("Protected page");
    expect(table).toHaveTextContent("protected_page");
    expect(within(table).getByRole("link", { name: "Investigate" })).toHaveAttribute(
      "href",
      `/autonomy/blocked/${PLAN_ID}`,
    );
    expect(within(table).queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.getByText(/never overridable into a publish/i)).toBeInTheDocument();
  });

  it("filters by block code and sends only a contract code", async () => {
    await renderPage(BlockedActionsPage({ searchParams: Promise.resolve({ code: "stale_vector" }) }));
    expect(api.listBlockedActions).toHaveBeenCalledWith({ code: "stale_vector", limit: 50, offset: 0 });
    await renderPage(BlockedActionsPage({ searchParams: Promise.resolve({ code: "toString" }) }));
    expect(api.listBlockedActions).toHaveBeenLastCalledWith({ code: null, limit: 50, offset: 0 });
  });

  it("tells a role without `deploy` that it may investigate but not override", async () => {
    as("reviewer");
    await renderPage(BlockedActionsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByText(/needs the deploy permission, which your role does not have/i)).toBeInTheDocument();
  });

  // A newer backend may add a block code this build has never heard of. The
  // console must show and flag it, because the API enforces it either way; the
  // cast is how a value outside the frozen enum is simulated.
  it("flags a block code this build does not know rather than hiding it", async () => {
    api.listBlockedActions.mockResolvedValue(
      ok(list([{ ...blockedAction(), blocks: ["teleported"] } as unknown as ReturnType<typeof blockedAction>])),
    );
    await renderPage(BlockedActionsPage({ searchParams: Promise.resolve({}) }));
    const table = screen.getByRole("table", { name: /Blocked actions/ });
    expect(table).toHaveTextContent("teleported");
    expect(table).toHaveTextContent("unknown to this console");
  });
});

describe("/autonomy/blocked/[planId] investigation view", () => {
  async function renderDetail(planId = PLAN_ID) {
    return renderPage(BlockedActionDetailPage({ params: Promise.resolve({ planId }) }));
  }

  it("shows the decision, the blocks with what to check, the score breakdown and the attempts", async () => {
    await renderDetail();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Why “lithium-ion battery market” did not publish");
    // The decision record.
    const decision = screen.getByRole("heading", { level: 2, name: "The autonomy decision" }).closest("section")!;
    expect(decision).toHaveTextContent("Block");
    expect(decision).toHaveTextContent("Medium risk");
    expect(decision).toHaveTextContent("0.630");
    expect(decision).toHaveTextContent("raw");
    expect(decision).toHaveTextContent("Yes — an inverse splice is possible");
    // The blocks, each with the investigation hint.
    const blocks = screen.getByRole("heading", { level: 2, name: "Why it is blocked" }).closest("section")!;
    expect(blocks).toHaveTextContent("Protected page");
    expect(blocks).toHaveTextContent("What to check:");
    // The per-signal score breakdown, never one opaque number.
    const score = screen.getByRole("table", { name: /Score breakdown/ });
    expect(score).toHaveTextContent("Semantic");
    expect(score).toHaveTextContent("Taxonomy");
    expect(score).toHaveTextContent("Relevance (sum of contributions)");
    // The anchor rationale.
    expect(screen.getByRole("heading", { level: 2, name: "Anchor rationale" })).toBeInTheDocument();
    // Graph diagnostics for the destination.
    expect(screen.getByRole("table", { name: /Link-graph position/ })).toHaveTextContent("Topical cluster");
    // The attempt ledger for this plan.
    expect(screen.getByRole("table", { name: /Publish attempts for this plan/ })).toBeInTheDocument();
  });

  it("says unmistakably that a shadow decision did not happen", async () => {
    await renderDetail();
    const notes = screen.getAllByRole("note").map((note) => note.textContent ?? "");
    expect(notes.some((text) => text.includes("This did not happen."))).toBe(true);
    expect(notes.some((text) => text.includes("nothing was published"))).toBe(true);
    expect(notes.some((text) => text.includes("would have been “Auto-publish”"))).toBe(true);
  });

  it("offers the override to `deploy`, marked as an exception, and names the unoverridable blocks", async () => {
    await renderDetail();
    const panel = screen.getByRole("heading", { level: 2, name: /Operator override/ }).closest("section")!;
    expect(panel).toHaveTextContent("an exception, not the normal path");
    expect(panel).toHaveTextContent("needs no approval");
    expect(panel).toHaveTextContent("A deterministic block is never overridable into a publish.");
    expect(panel).toHaveTextContent("Protected page");
    expect(within(panel).getByLabelText("Action (required)")).toBeInTheDocument();
    expect(within(panel).getByLabelText("Reason (required)")).toBeRequired();
    expect(within(panel).getByRole("button", { name: "Record override" })).toBeInTheDocument();
  });

  it("mints exactly one operation id per rendered override form", async () => {
    const { container } = await renderDetail();
    const ids = [...container.querySelectorAll('input[name="operation_id"]')].map((input) =>
      input.getAttribute("value"),
    );
    expect(ids).toHaveLength(1);
    expect(ids[0]).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    // The form carries the status it was built from, so a stale form is refused.
    expect(container.querySelector('input[name="expected_status"]')).toHaveAttribute("value", "blocked");
  });

  it("hides the override from a role without `deploy` and names the permission", async () => {
    as("reviewer");
    await renderDetail();
    expect(screen.queryByRole("button", { name: "Record override" })).toBeNull();
    expect(screen.getByText(/needs the deploy permission/i)).toBeInTheDocument();
  });

  it("404s a malformed plan id without calling the API", async () => {
    await expect(renderDetail("../evil")).rejects.toThrow("NEXT_NOT_FOUND");
    expect(api.getLinkPlan).not.toHaveBeenCalled();
  });

  it("says so when no autonomy decision has been recorded", async () => {
    api.listAutonomyDecisions.mockResolvedValue(ok(list([])));
    await renderDetail();
    expect(screen.getByText(/No autonomy decision has been recorded for this plan/)).toBeInTheDocument();
    // With no decision there is no block set either; the empty list is explained, not shown as "all clear".
    expect(screen.getByText(/not a safety refusal/i)).toBeInTheDocument();
  });

  it("warns prominently when a rollback is not possible", async () => {
    api.listAutonomyDecisions.mockResolvedValue(
      ok(list([autonomyDecision({ rollback_available: false, blocks: ["missing_rollback_metadata"] })])),
    );
    await renderDetail();
    expect(screen.getByText(/nothing is published that cannot be undone/i)).toBeInTheDocument();
  });
});

describe("block reason and deferral", () => {
  const HARD = "blocked: deterministic block protected_page (the destination is protected)";
  const DEFER =
    "blocked: deferred -- calibration absent (raw confidence); risk tier medium (not low); confidence 0.42 below the guarded floor 0.85";

  it("shows a hard block's reason as text and labels it a hard block", async () => {
    api.listBlockedActions.mockResolvedValue(ok(list([blockedAction({ reason: HARD })])));
    await renderPage(BlockedActionsPage({ searchParams: Promise.resolve({}) }));
    const table = screen.getByRole("table", { name: /Blocked actions/ });
    expect(table).toHaveTextContent("Hard block");
    expect(table).toHaveTextContent(HARD);
    expect(table).not.toHaveTextContent("still override");
  });

  it("labels a deferral as one, shows its reason and says it can be overridden", async () => {
    api.listBlockedActions.mockResolvedValue(ok(list([blockedAction({ blocks: [], reason: DEFER })])));
    const { container } = await renderPage(BlockedActionsPage({ searchParams: Promise.resolve({}) }));
    const table = screen.getByRole("table", { name: /Blocked actions/ });
    expect(table).toHaveTextContent("Deferred");
    expect(table).not.toHaveTextContent("Hard block");
    expect(table).toHaveTextContent(DEFER);
    expect(table).toHaveTextContent("A deferral, not a hard block: an operator may still override it.");
    expect(container.querySelector("ul")).toBeNull();
  });

  it("renders a reason containing markup as text", async () => {
    api.listBlockedActions.mockResolvedValue(
      ok(list([blockedAction({ reason: "<img src=x onerror=alert(1)>blocked" })])),
    );
    const { container } = await renderPage(BlockedActionsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("table", { name: /Blocked actions/ })).toHaveTextContent("<img src=x");
    expect(container.querySelector("img")).toBeNull();
  });

  it("says plainly when no reason was recorded", async () => {
    await renderPage(BlockedActionsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("table", { name: /Blocked actions/ })).toHaveTextContent("No reason was recorded.");
  });

  it("the investigation view shows the reason and, for a deferral, that it is overridable", async () => {
    api.listAutonomyDecisions.mockResolvedValue(ok(list([autonomyDecision({ blocks: [] })])));
    api.listBlockedActions.mockResolvedValue(ok(list([blockedAction({ blocks: [], reason: DEFER })])));
    await renderPage(BlockedActionDetailPage({ params: Promise.resolve({ planId: PLAN_ID }) }));
    const section = screen.getByRole("heading", { level: 2, name: "Why it is blocked" }).closest("section")!;
    expect(section).toHaveTextContent(DEFER);
    expect(section).toHaveTextContent("not a safety refusal");
    expect(section).not.toHaveTextContent("recorded no block code");
    const panel = screen.getByRole("heading", { level: 2, name: /Operator override/ }).closest("section")!;
    expect(panel).toHaveTextContent("This is a deferral, not a hard block.");
    expect(panel).toHaveTextContent("a “publish” override can succeed");
  });

  it("the investigation view of a hard block still says it is not overridable", async () => {
    api.listBlockedActions.mockResolvedValue(ok(list([blockedAction({ reason: HARD })])));
    await renderPage(BlockedActionDetailPage({ params: Promise.resolve({ planId: PLAN_ID }) }));
    expect(screen.getByRole("heading", { level: 2, name: "Why it is blocked" }).closest("section")).toHaveTextContent(HARD);
    const panel = screen.getByRole("heading", { level: 2, name: /Operator override/ }).closest("section")!;
    expect(panel).not.toHaveTextContent("This is a deferral");
    expect(panel).toHaveTextContent("never overridable into a publish");
  });
});

describe("/autonomy/attempts", () => {
  it("shows what published, what rolled back and what failed", async () => {
    api.listPublishAttempts.mockResolvedValue(
      ok(
        list([
          publishAttempt(),
          publishAttempt({ id: 2, rolled_back: true }),
          publishAttempt({ id: 3, succeeded: false, failure_code: "cms_unavailable", cooldown_until: "2099-01-01T00:00:00Z" }),
          publishAttempt({ id: 4, mode: "shadow" }),
        ]),
      ),
    );
    await renderPage(PublishAttemptsPage({ searchParams: Promise.resolve({}) }));
    const rows = within(screen.getByRole("table", { name: /Publish attempts/ })).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Published");
    expect(rows[1]).toHaveTextContent("Rolled back");
    expect(rows[2]).toHaveTextContent("Failed");
    expect(rows[2]).toHaveTextContent("cms_unavailable");
    expect(rows[2]).toHaveTextContent("In cool-down until");
    // A shadow attempt is never shown as something that happened.
    expect(rows[3]).toHaveTextContent("Shadow — did not happen");
  });

  it("says the autonomous rollback rule in the page text", async () => {
    await renderPage(PublishAttemptsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByText(/rolls back by itself when that is safe/i)).toBeInTheDocument();
    expect(screen.getByText(/cool-down so the same plan\s+is not retried in a loop/i)).toBeInTheDocument();
  });
});

describe("/system", () => {
  it("names Qdrant, Ollama and the CMS, and shows the autonomy mode", async () => {
    api.getReady.mockResolvedValue(
      ok({
        status: "degraded",
        identity: { service: "ken-api", version: "0.1.0", environment: "local", git_sha: null },
        timestamp: "2026-10-16T10:00:00Z",
        duration_ms: 12,
        checks: [
          {
            name: "qdrant",
            kind: "vector_store",
            required: false,
            status: "ok",
            latency_ms: 4,
            detail: { collection: "pages", points: 65 },
            error: null,
          },
          { name: "ollama", kind: "model_server", required: false, status: "unavailable", latency_ms: 0, error: "refused" },
          { name: "cms", kind: "cms", required: true, status: "ok", latency_ms: 2, error: null },
        ],
      }),
    );
    api.getMeta.mockResolvedValue(fail({ path: "/meta" }));
    await renderPage(SystemPage());
    const section = screen.getByRole("heading", { level: 2, name: /Vector store, model server and CMS/ }).closest("section")!;
    expect(section).toHaveTextContent("qdrant");
    expect(section).toHaveTextContent("ollama");
    expect(section).toHaveTextContent("Unavailable");
    expect(section).toHaveTextContent("collection: pages");
    expect(section).toHaveTextContent("cms");
    expect(screen.getAllByRole("note", { name: "Autonomy mode" })[0]).toHaveTextContent("Shadow");
  });

  it("says a key service was not reported rather than omitting it", async () => {
    api.getReady.mockResolvedValue(
      ok({
        status: "ready",
        identity: { service: "ken-api", version: "0.1.0", environment: "local", git_sha: null },
        timestamp: "2026-10-16T10:00:00Z",
        duration_ms: 12,
        checks: [{ name: "db", kind: "database", required: true, status: "ok", latency_ms: 1, error: null }],
      }),
    );
    api.getMeta.mockResolvedValue(fail({ path: "/meta" }));
    await renderPage(SystemPage());
    const section = screen.getByRole("heading", { level: 2, name: /Vector store, model server and CMS/ }).closest("section")!;
    expect(section.textContent?.match(/Not reported by this API build/g)).toHaveLength(3);
  });
});
