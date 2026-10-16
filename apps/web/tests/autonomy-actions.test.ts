// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from "vitest";

import { overrideAction } from "@/app/autonomy/actions";
import type { ApiError, ApiResult } from "@/lib/api-client";
import { OVERRIDE_IDLE } from "@/lib/autonomy";

import { PLAN_ID } from "./fixtures/recommendations";
import { OPERATION_ID } from "./fixtures/review";

const api = vi.hoisted(() => ({ overrideAutonomyDecision: vi.fn() }));
const cache = vi.hoisted(() => ({ revalidatePath: vi.fn() }));

vi.mock("@/lib/api", () => api);
vi.mock("next/cache", () => cache);

beforeEach(() => {
  api.overrideAutonomyDecision.mockReset();
  cache.revalidatePath.mockReset();
});

function form(fields: Record<string, string>): FormData {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) data.append(key, value);
  return data;
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, status: 200 };
}

function fail(error: Partial<ApiError>): ApiResult<never> {
  return {
    ok: false,
    error: { kind: "api_error", path: "/autonomy/plans/{plan_id}/override", message: "m", ...error },
  };
}

const BASE = {
  plan_id: PLAN_ID,
  operation_id: OPERATION_ID,
  action: "publish",
  reason: "The editor confirmed the destination is live.",
  expected_status: "blocked",
};

function response(overrides: Record<string, unknown> = {}) {
  return {
    plan_id: PLAN_ID,
    action: "publish" as const,
    applied: true,
    plan_status: "validated" as const,
    actor: "user:jane",
    refusal: null,
    blocks: [] as const,
    occurred_at: "2026-10-16T10:00:00Z",
    ...overrides,
  };
}

describe("overrideAction", () => {
  it("sends the form's operation id, the action, the reason and the status the operator saw", async () => {
    api.overrideAutonomyDecision.mockResolvedValue(ok(response()));
    const state = await overrideAction(OVERRIDE_IDLE, form(BASE));
    expect(state).toMatchObject({ status: "done", applied: true });
    const [planId, body] = api.overrideAutonomyDecision.mock.calls[0]!;
    expect(planId).toBe(PLAN_ID);
    expect(body).toEqual({
      operation_id: OPERATION_ID,
      action: "publish",
      reason: "The editor confirmed the destination is live.",
      expected_status: "blocked",
    });
    // The actor is the API's business, derived from the session.
    expect(body).not.toHaveProperty("actor");
  });

  it("reuses the FORM's operation id rather than minting a new one, so a retry replays", async () => {
    api.overrideAutonomyDecision.mockResolvedValue(ok(response()));
    await overrideAction(OVERRIDE_IDLE, form(BASE));
    await overrideAction(OVERRIDE_IDLE, form(BASE));
    expect(api.overrideAutonomyDecision.mock.calls[0]![1].operation_id).toBe(OPERATION_ID);
    expect(api.overrideAutonomyDecision.mock.calls[1]![1].operation_id).toBe(OPERATION_ID);
  });

  it("mints an id only when the form did not carry a usable one", async () => {
    api.overrideAutonomyDecision.mockResolvedValue(ok(response()));
    await overrideAction(OVERRIDE_IDLE, form({ ...BASE, operation_id: "not-a-uuid" }));
    expect(api.overrideAutonomyDecision.mock.calls[0]![1].operation_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

  it("refuses invalid input without calling the API", async () => {
    expect(await overrideAction(OVERRIDE_IDLE, form({ ...BASE, reason: "  " }))).toMatchObject({
      status: "invalid",
      fieldErrors: { reason: expect.stringMatching(/reason is required/i) },
    });
    expect(await overrideAction(OVERRIDE_IDLE, form({ ...BASE, action: "" }))).toMatchObject({ status: "invalid" });
    expect(await overrideAction(OVERRIDE_IDLE, form({ ...BASE, plan_id: "../evil" }))).toMatchObject({
      status: "invalid",
      message: expect.stringMatching(/out of date/i),
    });
    expect(api.overrideAutonomyDecision).not.toHaveBeenCalled();
  });

  it("reports a 200 with applied:false as a refusal, and refreshes the screens", async () => {
    api.overrideAutonomyDecision.mockResolvedValue(
      ok(response({ applied: false, plan_status: "blocked", blocks: ["protected_page"], refusal: "Still protected." })),
    );
    const state = await overrideAction(OVERRIDE_IDLE, form(BASE));
    expect(state.status).toBe("done");
    if (state.status !== "done") throw new Error("unreachable");
    expect(state.applied).toBe(false);
    expect(state.message).toMatch(/REFUSED and nothing changed/);
    expect(state.message).toMatch(/never overridable/i);
    expect(cache.revalidatePath).toHaveBeenCalledWith(`/autonomy/blocked/${PLAN_ID}`);
    expect(cache.revalidatePath).toHaveBeenCalledWith("/autonomy/blocked");
    expect(cache.revalidatePath).toHaveBeenCalledWith(`/recommendations/${PLAN_ID}`);
  });

  // THE PHASE 6 LESSON. A 503 means the intent may be recorded and the site may
  // already have changed, so the action must refresh and must not say otherwise.
  it("never claims nothing changed on a 503, and refreshes the affected screens", async () => {
    api.overrideAutonomyDecision.mockResolvedValue(fail({ kind: "http_status", status: 503 }));
    const state = await overrideAction(OVERRIDE_IDLE, form(BASE));
    expect(state).toMatchObject({ status: "error", outcomeUnknown: true });
    if (state.status !== "error") throw new Error("unreachable");
    expect(state.message).toMatch(/UNKNOWN/);
    expect(state.message).not.toMatch(/nothing was changed/i);
    expect(cache.revalidatePath).toHaveBeenCalledWith("/autonomy/attempts");
  });

  it("treats a timeout the same way", async () => {
    api.overrideAutonomyDecision.mockResolvedValue(fail({ kind: "timeout" }));
    const state = await overrideAction(OVERRIDE_IDLE, form(BASE));
    expect(state).toMatchObject({ status: "error", outcomeUnknown: true });
    expect(cache.revalidatePath).toHaveBeenCalled();
  });

  it("reports a 409 as a known refusal and refreshes, so the real status shows", async () => {
    api.overrideAutonomyDecision.mockResolvedValue(fail({ status: 409, code: "conflict", detail: "Already rejected." }));
    const state = await overrideAction(OVERRIDE_IDLE, form(BASE));
    expect(state).toMatchObject({ status: "error", outcomeUnknown: false, code: "conflict" });
    if (state.status !== "error") throw new Error("unreachable");
    expect(state.message).toMatch(/no longer in the status this form was built from/i);
    expect(cache.revalidatePath).toHaveBeenCalledWith("/autonomy/blocked");
  });

  it("does not refresh anything on a 403, because nothing can have changed", async () => {
    api.overrideAutonomyDecision.mockResolvedValue(fail({ status: 403, code: "permission_denied" }));
    const state = await overrideAction(OVERRIDE_IDLE, form(BASE));
    expect(state).toMatchObject({ status: "error", outcomeUnknown: false });
    if (state.status !== "error") throw new Error("unreachable");
    expect(state.message).toMatch(/deploy permission/);
    expect(cache.revalidatePath).not.toHaveBeenCalled();
  });
});
