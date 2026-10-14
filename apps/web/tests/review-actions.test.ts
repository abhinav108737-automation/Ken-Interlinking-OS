// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  approveAction,
  bulkReviewAction,
  editAnchorAction,
  rejectAction,
  replaceTargetAction,
} from "@/app/recommendations/actions";
import type { ApiError, ApiResult } from "@/lib/api-client";
import { IDLE } from "@/lib/review";

import { OLD_PLAN_ID, PLAN_ID } from "./fixtures/recommendations";
import { NEW_PLAN_ID, THIRD_PAGE_ID, bulkResult, reviewResult } from "./fixtures/review";
import { redirectTarget } from "./redirect";

const api = vi.hoisted(() => ({
  approvePlan: vi.fn(),
  rejectPlan: vi.fn(),
  editPlanAnchor: vi.fn(),
  replacePlanTarget: vi.fn(),
  bulkReviewPlans: vi.fn(),
}));
const cache = vi.hoisted(() => ({ revalidatePath: vi.fn() }));

vi.mock("@/lib/api", () => api);
vi.mock("next/cache", () => cache);

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const HASH = "d".repeat(64);

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
  return { ok: false, error: { kind: "api_error", path: "/recommendations/{plan_id}/approve", message: "m", ...error } };
}

const BASE = { plan_id: PLAN_ID, expected_status: "awaiting_approval" };

describe("approveAction", () => {
  it("sends a fresh operation id and the status the reviewer saw", async () => {
    api.approvePlan.mockResolvedValue(ok(reviewResult()));
    const first = await approveAction(IDLE, form({ ...BASE, reasons: "good_as_is", note: "Fine." }));
    await approveAction(IDLE, form({ ...BASE }));
    expect(first).toMatchObject({ status: "success" });
    if (first.status !== "success") throw new Error("unreachable");
    expect(first.message).toMatch(/Approved\. Status: Approved\. Nothing is deployed/);
    const [planId, body] = api.approvePlan.mock.calls[0]!;
    expect(planId).toBe(PLAN_ID);
    expect(body).toEqual({ operation_id: expect.stringMatching(UUID), expected_status: "awaiting_approval", reasons: ["good_as_is"], note: "Fine." });
    expect(body).not.toHaveProperty("actor");
    expect(api.approvePlan.mock.calls[1]![1].operation_id).not.toBe(body.operation_id);
    expect(cache.revalidatePath).toHaveBeenCalledWith(`/recommendations/${PLAN_ID}`);
  });

  it("reports an idempotent replay", async () => {
    api.approvePlan.mockResolvedValue(ok(reviewResult({ idempotent_replay: true })));
    const state = await approveAction(IDLE, form(BASE));
    expect(state.status === "success" && state.message).toMatch(/repeated an earlier identical request/);
  });

  it("refuses invalid input without calling the API", async () => {
    expect(await approveAction(IDLE, form({ ...BASE, reasons: "over_linking" }))).toMatchObject({ status: "invalid" });
    expect(await approveAction(IDLE, form({ ...BASE, reasons: "other", note: " " }))).toMatchObject({
      status: "invalid",
      fieldErrors: { note: expect.stringMatching(/Other/) },
    });
    expect(await approveAction(IDLE, form({ plan_id: "../x", expected_status: "awaiting_approval" }))).toMatchObject({ status: "invalid" });
    expect(await approveAction(IDLE, form({ plan_id: PLAN_ID, expected_status: "bogus" }))).toMatchObject({ status: "invalid" });
    expect(api.approvePlan).not.toHaveBeenCalled();
  });

  it.each([
    [409, "stale_page_version", /source page has changed/],
    [409, "conflict", /changed since you loaded it/],
    [403, "permission_denied", /role does not permit/],
    [422, "validation_failed", /rejected the input/],
  ])("maps HTTP %i %s to a typed message", async (status, code, message) => {
    api.approvePlan.mockResolvedValue(fail({ status, code: code as NonNullable<ApiError["code"]> }));
    const state = await approveAction(IDLE, form(BASE));
    expect(state).toMatchObject({ status: "error", code, message: expect.stringMatching(message) });
    expect(cache.revalidatePath).not.toHaveBeenCalled();
  });
});

describe("rejectAction", () => {
  it("requires a reject reason and sends it", async () => {
    expect(await rejectAction(IDLE, form(BASE))).toMatchObject({ status: "invalid", fieldErrors: { reasons: expect.any(String) } });
    expect(await rejectAction(IDLE, form({ ...BASE, reasons: "good_as_is" }))).toMatchObject({ status: "invalid" });
    api.rejectPlan.mockResolvedValue(ok(reviewResult({ action: "reject", status: "rejected" })));
    const state = await rejectAction(IDLE, form({ ...BASE, expected_status: "approved", reasons: ["over_linking", "other"], note: "Too many." }));
    expect(state).toMatchObject({ status: "success" });
    expect(api.rejectPlan.mock.calls[0]![1]).toMatchObject({ expected_status: "approved", reasons: ["over_linking", "other"], note: "Too many." });
  });
});

describe("editAnchorAction", () => {
  const EDIT = { ...BASE, reasons: "unnatural_anchor", start: "11", end: "37", block_context_hash: HASH };

  it("sends the span, the context hash and approve-now, then opens the new plan", async () => {
    api.editPlanAnchor.mockResolvedValue(ok(reviewResult({ action: "edit_anchor", status: "superseded", new_plan_id: NEW_PLAN_ID, new_plan_status: "approved" })));
    const target = await redirectTarget(() => editAnchorAction(IDLE, form({ ...EDIT, approve: "on" })));
    expect(target).toBe(`/recommendations/${NEW_PLAN_ID}?edited=1`);
    expect(api.editPlanAnchor.mock.calls[0]).toEqual([
      PLAN_ID,
      {
        operation_id: expect.stringMatching(UUID),
        expected_status: "awaiting_approval",
        reasons: ["unnatural_anchor"],
        note: "",
        block_context_hash: HASH,
        start: 11,
        end: 37,
        approve: true,
      },
    ]);
  });

  it("refuses a bad span, hash or reason locally", async () => {
    expect(await editAnchorAction(IDLE, form({ ...EDIT, start: "37", end: "11" }))).toMatchObject({ fieldErrors: { span: expect.any(String) } });
    expect(await editAnchorAction(IDLE, form({ ...EDIT, start: "-1" }))).toMatchObject({ status: "invalid" });
    expect(await editAnchorAction(IDLE, form({ ...EDIT, block_context_hash: "nope" }))).toMatchObject({ status: "invalid" });
    expect(await editAnchorAction(IDLE, form({ ...EDIT, reasons: "irrelevant_destination" }))).toMatchObject({ status: "invalid" });
    expect(api.editPlanAnchor).not.toHaveBeenCalled();
  });

  it("explains a refused span with the API's reason", async () => {
    api.editPlanAnchor.mockResolvedValue(fail({ status: 422, code: "validation_failed", detail: "The span overlaps an existing link." }));
    const state = await editAnchorAction(IDLE, form(EDIT));
    expect(state).toMatchObject({ status: "error", message: expect.stringMatching(/overlaps an existing link/) });
  });
});

describe("replaceTargetAction", () => {
  it("requires a destination and a destination reason", async () => {
    expect(await replaceTargetAction(IDLE, form({ ...BASE, reasons: "better_destination_exists" }))).toMatchObject({
      fieldErrors: { target: expect.any(String) },
    });
    expect(await replaceTargetAction(IDLE, form({ ...BASE, reasons: "unnatural_anchor", target_page_id: THIRD_PAGE_ID }))).toMatchObject({
      status: "invalid",
    });
    api.replacePlanTarget.mockResolvedValue(ok(reviewResult({ action: "replace_target", status: "superseded", new_plan_id: NEW_PLAN_ID })));
    const target = await redirectTarget(() =>
      replaceTargetAction(IDLE, form({ ...BASE, reasons: "better_destination_exists", target_page_id: THIRD_PAGE_ID.toUpperCase() })),
    );
    expect(target).toBe(`/recommendations/${NEW_PLAN_ID}?edited=1`);
    expect(api.replacePlanTarget.mock.calls[0]![1]).toMatchObject({ target_page_id: THIRD_PAGE_ID, approve: false });
  });
});

describe("bulkReviewAction", () => {
  it("validates selection size and reasons", async () => {
    expect(await bulkReviewAction(IDLE, form({ bulk_action: "approve" }))).toMatchObject({ fieldErrors: { plans: expect.any(String) } });
    expect(await bulkReviewAction(IDLE, form({ bulk_action: "reject", plan_ids: PLAN_ID }))).toMatchObject({
      fieldErrors: { reasons: expect.any(String) },
    });
    const many = Array.from({ length: 51 }, (_, i) => `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`);
    expect(await bulkReviewAction(IDLE, form({ bulk_action: "approve", plan_ids: many }))).toMatchObject({
      fieldErrors: { plans: expect.stringMatching(/at most 50/) },
    });
    expect(await bulkReviewAction(IDLE, form({ bulk_action: "deploy", plan_ids: PLAN_ID }))).toMatchObject({ status: "invalid" });
    expect(api.bulkReviewPlans).not.toHaveBeenCalled();
  });

  it("returns per-plan results and a tally", async () => {
    api.bulkReviewPlans.mockResolvedValue(ok(bulkResult()));
    const state = await bulkReviewAction(IDLE, form({ bulk_action: "approve", plan_ids: [PLAN_ID, OLD_PLAN_ID] }));
    expect(state).toMatchObject({ status: "done", message: "1 of 2 approved; 1 not changed (see below)." });
    expect(api.bulkReviewPlans.mock.calls[0]![0]).toEqual({
      operation_id: expect.stringMatching(UUID),
      action: "approve",
      plan_ids: [PLAN_ID, OLD_PLAN_ID],
      reasons: [],
      note: "",
    });
  });
});
