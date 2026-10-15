// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from "vitest";

import { deployAction, dryRunAction, rollbackAction, verifyAction } from "@/app/deployments/actions";
import type { ApiError, ApiResult } from "@/lib/api-client";
import { IDLE } from "@/lib/deployments";

import { DEPLOYMENT_ID, deploymentDetail, dryRunResult } from "./fixtures/deployments";
import { OLD_PLAN_ID, PLAN_ID } from "./fixtures/recommendations";
import { OPERATION_ID } from "./fixtures/review";
import { redirectTarget } from "./redirect";

const api = vi.hoisted(() => ({
  dryRunDeployment: vi.fn(),
  createDeployment: vi.fn(),
  verifyDeployment: vi.fn(),
  rollbackDeployment: vi.fn(),
}));
const cache = vi.hoisted(() => ({ revalidatePath: vi.fn() }));

vi.mock("@/lib/api", () => api);
vi.mock("next/cache", () => cache);

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  cache.revalidatePath.mockReset();
});

function form(fields: Record<string, string | string[]>): FormData {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) {
    for (const item of Array.isArray(value) ? value : [value]) data.append(key, item);
  }
  return data;
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, status: 200 };
}

function fail(error: Partial<ApiError>): ApiResult<never> {
  return { ok: false, error: { kind: "api_error", path: "/deployments", message: "m", ...error } };
}

describe("dryRunAction", () => {
  it("sends the plans with a fresh operation id every time and returns the preview", async () => {
    api.dryRunDeployment.mockResolvedValue(ok(dryRunResult()));
    const first = await dryRunAction(IDLE, form({ plan_ids: [PLAN_ID.toUpperCase(), OLD_PLAN_ID], operation_id: OPERATION_ID }));
    const second = await dryRunAction(IDLE, form({ plan_ids: PLAN_ID }));
    expect(first).toMatchObject({ status: "preview", planIds: [PLAN_ID, OLD_PLAN_ID], result: dryRunResult() });
    const [body] = api.dryRunDeployment.mock.calls[0]!;
    expect(body).toEqual({ operation_id: expect.stringMatching(UUID), plan_ids: [PLAN_ID, OLD_PLAN_ID], note: "" });
    // A dry run never consumes an operation id: the form's is not reused.
    expect(body.operation_id).not.toBe(OPERATION_ID);
    expect(api.dryRunDeployment.mock.calls[1]![0].operation_id).not.toBe(body.operation_id);
    if (first.status !== "preview" || second.status !== "preview") throw new Error("unreachable");
    expect(first.key).not.toBe(second.key);
    expect(cache.revalidatePath).not.toHaveBeenCalled();
  });

  it("refuses an invalid selection without calling the API", async () => {
    expect(await dryRunAction(IDLE, form({}))).toMatchObject({ status: "invalid", fieldErrors: { plans: expect.any(String) } });
    expect(await dryRunAction(IDLE, form({ plan_ids: "../x" }))).toMatchObject({ status: "invalid" });
    const many = Array.from({ length: 21 }, (_, i) => `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`);
    expect(await dryRunAction(IDLE, form({ plan_ids: many }))).toMatchObject({ fieldErrors: { plans: expect.stringMatching(/at most 20/) } });
    expect(api.dryRunDeployment).not.toHaveBeenCalled();
  });

  it("returns typed errors with issues", async () => {
    api.dryRunDeployment.mockResolvedValue(fail({ status: 409, code: "conflict", details: [{ code: "deployment_in_progress", location: null }] }));
    expect(await dryRunAction(IDLE, form({ plan_ids: PLAN_ID }))).toMatchObject({
      status: "error",
      code: "conflict",
      message: expect.stringMatching(/Retry later/),
      issues: [{ code: "deployment_in_progress", planId: null }],
    });
  });
});

describe("deployAction", () => {
  it("sends the form-minted operation id, plans and note, then opens the deployment", async () => {
    api.createDeployment.mockResolvedValue(ok(deploymentDetail()));
    const target = await redirectTarget(() =>
      deployAction(IDLE, form({ operation_id: OPERATION_ID.toUpperCase(), plan_ids: [PLAN_ID], note: "Release 12." })),
    );
    expect(target).toBe(`/deployments/${DEPLOYMENT_ID}?deployed=1`);
    expect(api.createDeployment.mock.calls[0]![0]).toEqual({ operation_id: OPERATION_ID, plan_ids: [PLAN_ID], note: "Release 12." });
    expect(api.createDeployment.mock.calls[0]![0]).not.toHaveProperty("actor");
    expect(cache.revalidatePath).toHaveBeenCalledWith(`/recommendations/${PLAN_ID}`);
    expect(cache.revalidatePath).toHaveBeenCalledWith("/deployments");
  });

  it("a retry of the same form replays with the same operation id", async () => {
    api.createDeployment.mockResolvedValueOnce({ ok: false, error: { kind: "timeout", path: "/deployments", message: "t" } });
    api.createDeployment.mockResolvedValueOnce(ok(deploymentDetail({ idempotent_replay: true })));
    const fields = { operation_id: OPERATION_ID, plan_ids: [PLAN_ID] };
    expect(await deployAction(IDLE, form(fields))).toMatchObject({ status: "error", message: expect.stringMatching(/same form again is safe/) });
    await redirectTarget(() => deployAction(IDLE, form(fields)));
    expect(api.createDeployment.mock.calls.map((args) => args[0].operation_id)).toEqual([OPERATION_ID, OPERATION_ID]);
  });

  it("mints an id only when the form's is missing or malformed", async () => {
    api.createDeployment.mockResolvedValue(ok(deploymentDetail()));
    await redirectTarget(() => deployAction(IDLE, form({ operation_id: "not-a-uuid", plan_ids: [PLAN_ID] })));
    expect(api.createDeployment.mock.calls[0]![0].operation_id).toMatch(UUID);
  });

  it.each([
    [{ status: 409, code: "cms_version_conflict" }, /plans have expired/, true],
    [{ status: 409, code: "conflict", details: [{ code: "deployment_in_progress", location: null }] }, /Retry later/, false],
    [{ status: 503, code: "cms_unavailable" }, /CMS is unavailable/, false],
    [{ status: 403, code: "permission_denied" }, /operator admin/, false],
    [{ status: 422, code: "validation_failed" }, /rejected the input/, false],
  ] as const)("maps %o to a typed error", async (error, message, refreshes) => {
    api.createDeployment.mockResolvedValue(fail(error as Partial<ApiError>));
    const state = await deployAction(IDLE, form({ operation_id: OPERATION_ID, plan_ids: [PLAN_ID] }));
    expect(state).toMatchObject({ status: "error", code: error.code, message: expect.stringMatching(message) });
    expect(cache.revalidatePath.mock.calls.length > 0).toBe(refreshes);
  });

  it("validates the note and the selection locally", async () => {
    expect(await deployAction(IDLE, form({ operation_id: OPERATION_ID, plan_ids: [PLAN_ID], note: "x".repeat(501) }))).toMatchObject({
      fieldErrors: { note: expect.any(String) },
    });
    expect(await deployAction(IDLE, form({ operation_id: OPERATION_ID, plan_ids: [PLAN_ID, PLAN_ID] }))).toMatchObject({
      fieldErrors: { plans: expect.stringMatching(/twice/) },
    });
    expect(api.createDeployment).not.toHaveBeenCalled();
  });
});

describe("verifyAction", () => {
  it("re-verifies and reports the new status", async () => {
    api.verifyDeployment.mockResolvedValue(ok(deploymentDetail({ status: "verification_failed" })));
    const state = await verifyAction(IDLE, form({ deployment_id: DEPLOYMENT_ID }));
    expect(state).toEqual({ status: "success", message: "Verification re-run. Deployment status: Verification failed." });
    expect(api.verifyDeployment).toHaveBeenCalledWith(DEPLOYMENT_ID);
    expect(cache.revalidatePath).toHaveBeenCalledWith(`/deployments/${DEPLOYMENT_ID}`);
  });

  it("refuses a malformed id", async () => {
    expect(await verifyAction(IDLE, form({ deployment_id: "../x" }))).toMatchObject({ status: "invalid" });
    expect(api.verifyDeployment).not.toHaveBeenCalled();
  });
});

describe("rollbackAction", () => {
  const BASE = { deployment_id: DEPLOYMENT_ID, operation_id: OPERATION_ID, eligible_plan_ids: [PLAN_ID, OLD_PLAN_ID] };

  it("requires a reason, and a note for 'other'", async () => {
    expect(await rollbackAction(IDLE, form(BASE))).toMatchObject({ status: "invalid", fieldErrors: { reason: expect.any(String) } });
    expect(await rollbackAction(IDLE, form({ ...BASE, reason: "other", note: " " }))).toMatchObject({
      status: "invalid",
      fieldErrors: { note: expect.stringMatching(/Other/) },
    });
    expect(await rollbackAction(IDLE, form({ ...BASE, reason: "bogus" }))).toMatchObject({ status: "invalid" });
    expect(api.rollbackDeployment).not.toHaveBeenCalled();
  });

  it("rolls back the whole deployment when no link is ticked", async () => {
    api.rollbackDeployment.mockResolvedValue(ok(deploymentDetail({ status: "rolled_back" })));
    const state = await rollbackAction(IDLE, form({ ...BASE, reason: "editorial_reversal" }));
    expect(state).toMatchObject({ status: "success", message: expect.stringMatching(/Rollback requested\. Deployment status: Rolled back\./) });
    expect(api.rollbackDeployment.mock.calls[0]).toEqual([
      DEPLOYMENT_ID,
      { operation_id: OPERATION_ID, reason: "editorial_reversal", note: "", plan_ids: null },
    ]);
  });

  it("sends the ticked subset for a per-plan rollback", async () => {
    api.rollbackDeployment.mockResolvedValue(ok(deploymentDetail({ status: "verified" })));
    const state = await rollbackAction(IDLE, form({ ...BASE, reason: "other", note: "Legal asked.", plan_ids: OLD_PLAN_ID }));
    expect(state).toMatchObject({ status: "success", message: expect.stringMatching(/for 1 link\./) });
    expect(api.rollbackDeployment.mock.calls[0]![1]).toEqual({ operation_id: OPERATION_ID, reason: "other", note: "Legal asked.", plan_ids: [OLD_PLAN_ID] });
  });

  it("explains a rollback conflict", async () => {
    api.rollbackDeployment.mockResolvedValue(fail({ status: 409, code: "conflict", details: [{ code: "rollback_conflict", location: PLAN_ID }] }));
    expect(await rollbackAction(IDLE, form({ ...BASE, reason: "broken_page" }))).toMatchObject({
      status: "error",
      message: expect.stringMatching(/was not removed/),
      issues: [{ code: "rollback_conflict", planId: PLAN_ID }],
    });
  });
});
