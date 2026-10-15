// @vitest-environment node
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import type { ApiError } from "@/lib/api-client";
import {
  DEPLOYMENT_ISSUE_CODES,
  DEPLOYMENT_STATUSES,
  ROLLBACK_REASONS,
  VERIFICATION_CHECK_NAMES,
  VERIFICATION_OUTCOMES,
  availableDeploymentActions,
  deploymentErrorMessage,
  deploymentHref,
  describeDeploymentErrorCode,
  describeDeploymentIssue,
  describeDeploymentStatus,
  describeRollbackReason,
  describeVerificationCheck,
  describeVerificationOutcome,
  describeVerificationRun,
  dryRunIssues,
  errorIssues,
  isDeploymentInProgress,
  previewHighlight,
  previewsInOrder,
  rollbackPlanIds,
  rollbackablePlans,
  validateNote,
  validatePlanSelection,
  validateRollbackInput,
  validateSameSource,
} from "@/lib/deployments";

import { DEPLOYMENT_ID, changePreview, dryRunResult, twoPlanDeployment, deploymentItem } from "./fixtures/deployments";
import { OLD_PLAN_ID, PLAN_ID } from "./fixtures/recommendations";

const openapi = JSON.parse(
  readFileSync(fileURLToPath(new URL("../../../contracts/openapi/openapi.json", import.meta.url)), "utf8"),
) as { components: { schemas: Record<string, { enum?: string[] }> } };

function contractEnum(name: string): string[] {
  const values = openapi.components.schemas[name]?.enum;
  if (values === undefined) throw new Error(`no enum ${name}`);
  return values;
}

const label = (value: string | { label: string }) => (typeof value === "string" ? value : value.label);

describe("label completeness against the contract", () => {
  it.each([
    ["DeploymentStatus", DEPLOYMENT_STATUSES, describeDeploymentStatus],
    ["DeploymentIssueCode", DEPLOYMENT_ISSUE_CODES, describeDeploymentIssue],
    ["VerificationCheckName", VERIFICATION_CHECK_NAMES, describeVerificationCheck],
    ["VerificationOutcome", VERIFICATION_OUTCOMES, describeVerificationOutcome],
    ["RollbackReason", ROLLBACK_REASONS, describeRollbackReason],
  ] as const)("labels every %s value, distinctly", (name, values, describe) => {
    expect([...values].sort()).toEqual([...contractEnum(name)].sort());
    const labels = values.map((value) => label(describe(value)));
    expect(labels.every((text) => !text.startsWith("Unknown"))).toBe(true);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("degrades unknown values to an explicit label", () => {
    for (const describe of [describeDeploymentStatus, describeDeploymentIssue, describeVerificationCheck, describeVerificationOutcome, describeRollbackReason, describeDeploymentErrorCode]) {
      expect(label(describe("from_the_future"))).toBe("Unknown (from_the_future)");
      expect(label(describe(""))).toBe("Unknown (empty)");
    }
    expect(describeDeploymentStatus("from_the_future")).toMatchObject({ known: false, tone: "neutral" });
  });

  it("distinguishes the in-flight, failed and rolled-back states in text", () => {
    expect(describeDeploymentStatus("applying").label).toBe("Applying");
    expect(describeDeploymentStatus("rolling_back").label).toBe("Rolling back");
    expect(describeDeploymentStatus("conflict").description).toMatch(/plans expired/);
    expect(describeVerificationOutcome("inconclusive")).toMatchObject({ label: "Inconclusive", tone: "caution" });
  });
});

describe("actions by status", () => {
  it.each([
    ["applied", true],
    ["verified", true],
    ["verification_failed", true],
    ["applying", false],
    ["rolling_back", false],
    ["failed", false],
    ["conflict", false],
    ["rolled_back", false],
    ["from_the_future", false],
  ])("%s → verify/rollback %s", (status, allowed) => {
    expect(availableDeploymentActions(status)).toEqual({ verify: allowed, rollback: allowed });
  });

  it("flags only applying and rolling_back as in progress", () => {
    expect(DEPLOYMENT_STATUSES.filter(isDeploymentInProgress)).toEqual(["applying", "rolling_back"]);
  });

  it("verification run verdicts come from the run's own flags", () => {
    expect(describeVerificationRun({ passed: true }).label).toBe("Passed");
    expect(describeVerificationRun({ passed: false, inconclusive: true }).label).toBe("Inconclusive");
    expect(describeVerificationRun({ passed: false }).label).toBe("Failed");
  });
});

describe("preview highlight (code-point offsets)", () => {
  it("splits at the anchor", () => {
    expect(previewHighlight(changePreview())).toEqual({
      before: "The global ",
      match: "lithium-ion battery market",
      after: " is growing as electric vehicles scale.",
    });
  });

  it("counts astral characters as one code point", () => {
    const text = "🔋⚡ Solid-state 電池 market 🚗 grows";
    // Code points: 🔋=0 ⚡=1 space=2, "Solid-state" = 3..14, space 14, 電池 15..17, space 17, market 18..24.
    expect(previewHighlight(changePreview({ block_text: text, anchor_text: "電池 market", anchor_start: 15, anchor_end: 24 }))).toEqual({
      before: "🔋⚡ Solid-state ",
      match: "電池 market",
      after: " 🚗 grows",
    });
    // UTF-16 offsets would select the wrong text: no highlight rather than a wrong one.
    expect(previewHighlight(changePreview({ block_text: text, anchor_text: "電池 market", anchor_start: 17, anchor_end: 26 }))).toBeNull();
  });

  it("refuses offsets that do not select the anchor verbatim", () => {
    expect(previewHighlight(changePreview({ anchor_start: 0, anchor_end: 3 }))).toBeNull();
    expect(previewHighlight(changePreview({ anchor_start: 11, anchor_end: 999 }))).toBeNull();
  });

  it("orders previews by the request's plan order", () => {
    const a = changePreview({ plan_id: OLD_PLAN_ID });
    const b = changePreview({ plan_id: PLAN_ID });
    expect(previewsInOrder([a, b], [PLAN_ID, OLD_PLAN_ID]).map((p) => p.plan_id)).toEqual([PLAN_ID, OLD_PLAN_ID]);
  });
});

function apiError(overrides: Partial<ApiError>): ApiError {
  return { kind: "api_error", path: "/deployments", message: "m", ...overrides };
}

describe("issues and error messages", () => {
  it("maps dry-run issues with their plans", () => {
    const issues = dryRunIssues(dryRunResult({ valid: false, issues: [{ code: "already_linked", plan_id: PLAN_ID.toUpperCase() }, { code: "mixed_sources", plan_id: null }] }));
    expect(issues).toEqual([
      { code: "already_linked", label: "Already linked", description: expect.any(String), planId: PLAN_ID },
      { code: "mixed_sources", label: "Different source pages", description: expect.any(String), planId: null },
    ]);
  });

  it("reads issues from a 409's detail codes, the plan from the location", () => {
    const error = apiError({ status: 409, code: "conflict", details: [{ code: "density_exceeded", location: null }, { code: "plan_stale", location: `plan_ids.${PLAN_ID}` }] });
    expect(errorIssues(error).map((issue) => [issue.code, issue.planId])).toEqual([
      ["density_exceeded", null],
      ["plan_stale", PLAN_ID],
    ]);
    expect(errorIssues(apiError({ status: 422, details: [{ code: "missing", location: "body.plan_ids" }] }))).toEqual([]);
  });

  it.each([
    [{ status: 409, code: "conflict", details: [{ code: "deployment_in_progress", location: null }] }, "deploy", /Retry later; the plans are unchanged/],
    [{ status: 409, code: "cms_version_conflict" }, "deploy", /plans have expired.*regenerates/],
    [{ status: 503, code: "cms_unavailable" }, "deploy", /CMS is unavailable/],
    [{ status: 403, code: "permission_denied" }, "deploy", /Only an operator admin/],
    [{ status: 422, code: "validation_failed", detail: "Bad note." }, "rollback", /rejected the input.*Bad note/],
    [{ status: 409, code: "conflict", details: [{ code: "rollback_conflict", location: PLAN_ID }] }, "rollback", /was not removed/],
    [{ status: 409, code: "conflict", details: [{ code: "nothing_to_roll_back", location: null }] }, "rollback", /nothing to roll back/],
    [{ status: 409, code: "conflict", details: [{ code: "already_linked", location: PLAN_ID }] }, "dry_run", /refused for the reasons listed/],
    [{ status: 409, code: "conflict" }, "deploy", /Reload and try again/],
    [{ kind: "timeout" as const }, "deploy", /same form again is safe/],
  ] as const)("explains %o", (overrides, operation, message) => {
    expect(deploymentErrorMessage(apiError(overrides as Partial<ApiError>), operation)).toMatch(message);
  });
});

describe("validation", () => {
  it("plan selection: 1–20 unique UUIDs", () => {
    expect(validatePlanSelection([])).toMatch(/at least one/);
    expect(validatePlanSelection([PLAN_ID])).toBeUndefined();
    expect(validatePlanSelection([PLAN_ID, PLAN_ID.toUpperCase()])).toMatch(/twice/);
    expect(validatePlanSelection(["../x"])).toMatch(/not valid/);
    const many = Array.from({ length: 21 }, (_, i) => `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`);
    expect(validatePlanSelection(many)).toMatch(/at most 20/);
  });

  it("one source page per deployment", () => {
    expect(validateSameSource([PLAN_ID, PLAN_ID.toUpperCase()])).toBeUndefined();
    expect(validateSameSource([PLAN_ID, OLD_PLAN_ID])).toMatch(/one source page/);
  });

  it("notes: length, unstorable characters", () => {
    expect(validateNote("fine\ttab\nnewline", false)).toBeUndefined();
    expect(validateNote("x".repeat(501), false)).toMatch(/at most 500/);
    for (const bad of ["a\u0000b", "a\u0007b", "a\u007Fb", "a\uD800b"]) expect(validateNote(bad, false)).toMatch(/invalid character/);
    expect(validateNote("🔋 ok", false)).toBeUndefined();
  });

  it("rollback: a reason is required, 'other' needs a note", () => {
    expect(validateRollbackInput({ reason: "", note: "", planIds: [] })).toEqual({ reason: expect.any(String) });
    expect(validateRollbackInput({ reason: "bogus", note: "", planIds: [] })).toEqual({ reason: expect.any(String) });
    expect(validateRollbackInput({ reason: "other", note: "  ", planIds: [] })).toEqual({ note: expect.stringMatching(/Other/) });
    expect(validateRollbackInput({ reason: "other", note: "Legal asked.", planIds: [] })).toEqual({});
    expect(validateRollbackInput({ reason: "broken_page", note: "", planIds: ["nope"] })).toEqual({ plans: expect.any(String) });
  });

  it("rollback plan ids: none or all ticked → the whole deployment", () => {
    expect(rollbackPlanIds([], [PLAN_ID, OLD_PLAN_ID])).toBeNull();
    expect(rollbackPlanIds([OLD_PLAN_ID, PLAN_ID], [PLAN_ID, OLD_PLAN_ID])).toBeNull();
    expect(rollbackPlanIds([OLD_PLAN_ID.toUpperCase()], [PLAN_ID, OLD_PLAN_ID])).toEqual([OLD_PLAN_ID]);
  });

  it("only links on the page can be rolled back", () => {
    const deployment = twoPlanDeployment({ items: [deploymentItem(), deploymentItem({ plan_id: OLD_PLAN_ID, plan_status: "rolled_back" })] });
    expect(rollbackablePlans(deployment).map((item) => item.plan_id)).toEqual([PLAN_ID]);
  });

  it("hrefs are encoded", () => {
    expect(deploymentHref(DEPLOYMENT_ID)).toBe(`/deployments/${DEPLOYMENT_ID}`);
    expect(deploymentHref("a/b")).toBe("/deployments/a%2Fb");
  });
});

describe("navigation", () => {
  it("lists Deployments for every role (it needs only `read`)", async () => {
    const { NAV_LINKS, navLinksFor, activeNavHref } = await import("@/lib/nav");
    expect(NAV_LINKS.find((link) => link.href === "/deployments")).toEqual({ href: "/deployments", label: "Deployments" });
    expect(navLinksFor(new Set(["read"])).map((link) => link.label)).toContain("Deployments");
    expect(activeNavHref(`/deployments/${DEPLOYMENT_ID}`)).toBe("/deployments");
  });
});
