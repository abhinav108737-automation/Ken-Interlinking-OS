// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  type ApiClient,
  type ApiResult,
  type FetchLike,
  createApiClient,
  isPermissionDeniedError,
  isUnauthenticatedError,
} from "@/lib/api-client";

import { OTHER_PAGE_ID, errorBody, jsonResponse, list } from "./fixtures/inventory";
import { PLAN_ID } from "./fixtures/recommendations";
import {
  OPERATION_ID,
  THIRD_PAGE_ID,
  TOKEN,
  bulkResult,
  dashboardSummary,
  eventRecord,
  policiesView,
  principal,
  reviewAction,
  reviewResult,
  sessionView,
  targetOption,
} from "./fixtures/review";

const BASE = "http://api.test:8000/prefix";
const HASH = "c".repeat(64);

let fetchMock: ReturnType<typeof vi.fn<FetchLike>>;
let client: ApiClient;

beforeEach(() => {
  fetchMock = vi.fn<FetchLike>();
  client = createApiClient({ baseUrl: BASE, fetch: fetchMock, token: TOKEN });
});

function call(index = 0): { url: URL; init: RequestInit; headers: Record<string, string> } {
  const args = fetchMock.mock.calls[index];
  if (args === undefined) throw new Error("fetch was not called");
  return { url: new URL(String(args[0])), init: args[1], headers: args[1].headers as Record<string, string> };
}

function expectError(result: ApiResult<unknown>) {
  expect(result.ok).toBe(false);
  if (result.ok) throw new Error("unreachable");
  return result.error;
}

const APPROVE = { operation_id: OPERATION_ID, expected_status: "awaiting_approval" as const, reasons: [], note: "" };
const REJECT = { ...APPROVE, reasons: ["irrelevant_destination" as const] };
const EDIT = { ...APPROVE, reasons: ["unnatural_anchor" as const], block_context_hash: HASH, start: 4, end: 10, approve: true };
const REPLACE = { ...APPROVE, reasons: ["better_destination_exists" as const], target_page_id: THIRD_PAGE_ID, approve: false };
const BULK = { operation_id: OPERATION_ID, action: "approve" as const, plan_ids: [PLAN_ID], reasons: [], note: "" };

interface Case {
  readonly name: string;
  readonly run: (c: ApiClient) => Promise<ApiResult<unknown>>;
  readonly method: "GET" | "POST";
  readonly path: string;
  readonly body?: unknown;
  readonly response: unknown;
  readonly status?: number;
}

const CASES: readonly Case[] = [
  { name: "getCurrentUser", run: (c) => c.getCurrentUser(), method: "GET", path: "/prefix/auth/me", response: principal() },
  { name: "getDashboard", run: (c) => c.getDashboard(), method: "GET", path: "/prefix/dashboard", response: dashboardSummary() },
  {
    name: "approvePlan",
    run: (c) => c.approvePlan(PLAN_ID, APPROVE),
    method: "POST",
    path: `/prefix/recommendations/${PLAN_ID}/approve`,
    body: APPROVE,
    response: reviewResult(),
  },
  {
    name: "rejectPlan",
    run: (c) => c.rejectPlan(PLAN_ID, REJECT),
    method: "POST",
    path: `/prefix/recommendations/${PLAN_ID}/reject`,
    body: REJECT,
    response: reviewResult({ action: "reject", status: "rejected" }),
  },
  {
    name: "editPlanAnchor",
    run: (c) => c.editPlanAnchor(PLAN_ID, EDIT),
    method: "POST",
    path: `/prefix/recommendations/${PLAN_ID}/edit-anchor`,
    body: EDIT,
    response: reviewResult({ action: "edit_anchor", status: "superseded", new_plan_id: OTHER_PAGE_ID, new_plan_status: "approved" }),
  },
  {
    name: "replacePlanTarget",
    run: (c) => c.replacePlanTarget(PLAN_ID, REPLACE),
    method: "POST",
    path: `/prefix/recommendations/${PLAN_ID}/replace-target`,
    body: REPLACE,
    response: reviewResult({ action: "replace_target", status: "superseded", new_plan_id: OTHER_PAGE_ID, new_plan_status: "awaiting_approval" }),
  },
  {
    name: "bulkReviewPlans",
    run: (c) => c.bulkReviewPlans(BULK),
    method: "POST",
    path: "/prefix/recommendations/bulk-review",
    body: BULK,
    response: bulkResult(),
  },
  {
    name: "listPlanTargetOptions",
    run: (c) => c.listPlanTargetOptions(PLAN_ID, { limit: 30 }),
    method: "GET",
    path: `/prefix/recommendations/${PLAN_ID}/target-options`,
    response: [targetOption()],
  },
  {
    name: "listPlanReviewActions",
    run: (c) => c.listPlanReviewActions(PLAN_ID),
    method: "GET",
    path: `/prefix/recommendations/${PLAN_ID}/review-actions`,
    response: [reviewAction()],
  },
  {
    name: "listReviewActions",
    run: (c) => c.listReviewActions({ action: "reject", actor: "user:jane", limit: 20, offset: 40 }),
    method: "GET",
    path: "/prefix/review/actions",
    response: list([reviewAction()]),
  },
  {
    name: "listEvents",
    run: (c) => c.listEvents({ event_name: "link.approved", subject_id: PLAN_ID, limit: 10 }),
    method: "GET",
    path: "/prefix/events",
    response: list([eventRecord()]),
  },
  { name: "getPolicies", run: (c) => c.getPolicies(), method: "GET", path: "/prefix/settings/policies", response: policiesView() },
];

describe.each(CASES)("$name", ({ run, method, path, body, response }) => {
  it("calls the contract URL with the bearer token and parses the body", async () => {
    fetchMock.mockResolvedValue(jsonResponse(response, 200));
    const result = await run(client);
    expect(result.ok).toBe(true);
    const { url, init, headers } = call();
    expect(url.pathname).toBe(path);
    expect(init.method).toBe(method);
    expect(init.redirect).toBe("manual");
    expect(headers["Authorization"]).toBe(`Bearer ${TOKEN}`);
    if (method === "POST") {
      expect(headers["Content-Type"]).toBe("application/json");
      expect(JSON.parse(String(init.body))).toEqual(body);
    } else {
      expect(init.body).toBeUndefined();
    }
  });

  it("maps 401 and 403 to api_error", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("unauthenticated"), 401));
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("permission_denied"), 403));
    const unauthenticated = expectError(await run(client));
    const denied = expectError(await run(client));
    expect(unauthenticated).toMatchObject({ kind: "api_error", status: 401, code: "unauthenticated" });
    expect(isUnauthenticatedError(unauthenticated)).toBe(true);
    expect(denied).toMatchObject({ kind: "api_error", status: 403, code: "permission_denied" });
    expect(isPermissionDeniedError(denied)).toBe(true);
    expect(denied.message).not.toContain("api.test");
  });

  it("rejects a body that does not match the contract", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ unexpected: true }, 200));
    expect(expectError(await run(client)).kind).toBe("invalid_body");
  });
});

describe("query parameters", () => {
  it("encodes the audit and target-option filters", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([]), 200));
    await client.listReviewActions({ action: "reject", actor: "user:jane", limit: 20, offset: 40 });
    expect(Object.fromEntries(call(0).url.searchParams)).toEqual({ action: "reject", actor: "user:jane", limit: "20", offset: "40" });
    await client.listEvents({ event_name: "link.approved", subject_id: null });
    expect(Object.fromEntries(call(1).url.searchParams)).toEqual({ event_name: "link.approved" });
    fetchMock.mockResolvedValue(jsonResponse([], 200));
    await client.listPlanTargetOptions(PLAN_ID, { limit: 30 });
    expect(call(2).url.searchParams.get("limit")).toBe("30");
  });
});

describe("mutation errors", () => {
  it("maps 409 stale_page_version and conflict with the API's detail", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "stale_page_version", message: "The source changed.", request_id: "r1" }, 409));
    const stale = expectError(await client.approvePlan(PLAN_ID, APPROVE));
    expect(stale).toMatchObject({ kind: "api_error", status: 409, code: "stale_page_version", detail: "The source changed.", requestId: "r1" });
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "conflict", message: "x".repeat(400) }, 409));
    const conflict = expectError(await client.rejectPlan(PLAN_ID, REJECT));
    expect(conflict.code).toBe("conflict");
    expect(conflict.detail?.length).toBe(300);
  });

  it("maps 422 and 404", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "validation_failed", message: "Span overlaps a link." }, 422));
    expect(expectError(await client.editPlanAnchor(PLAN_ID, EDIT))).toMatchObject({ status: 422, detail: "Span overlaps a link." });
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("not_found"), 404));
    expect(expectError(await client.replacePlanTarget(PLAN_ID, REPLACE))).toMatchObject({ status: 404, code: "not_found" });
  });

  it("never attaches the server message to a GET error", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "validation_failed", message: "bad" }, 422));
    expect(expectError(await client.listReviewActions()).detail).toBeUndefined();
  });

  it("refuses a malformed plan id without sending", async () => {
    for (const bad of ["../x", "a/b", "%2e%2e", ""]) {
      expect(expectError(await client.approvePlan(bad, APPROVE)).kind).toBe("invalid_argument");
      expect(expectError(await client.editPlanAnchor(bad, EDIT)).kind).toBe("invalid_argument");
      expect(expectError(await client.listPlanTargetOptions(bad)).kind).toBe("invalid_argument");
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("treats a timeout as a timeout (the change may have happened)", async () => {
    fetchMock.mockRejectedValue(new DOMException("timed out", "TimeoutError"));
    expect(expectError(await client.approvePlan(PLAN_ID, APPROVE)).kind).toBe("timeout");
  });
});

describe("auth operations", () => {
  it("login is public: no token, JSON credentials, 401 is a declared error", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(sessionView(), 200));
    const result = await client.login({ username: "jane", password: "pw" });
    expect(result.ok).toBe(true);
    expect(call().headers["Authorization"]).toBeUndefined();
    expect(JSON.parse(String(call().init.body))).toEqual({ username: "jane", password: "pw" });
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("unauthenticated"), 401));
    const error = expectError(await client.login({ username: "jane", password: "pw" }));
    expect(error).toMatchObject({ kind: "api_error", status: 401 });
    expect(error.message).toMatch(/not accepted/);
  });

  it("logout accepts an empty 204", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    const result = await client.logout();
    expect(result).toEqual({ ok: true, data: null, status: 204 });
    expect(call().headers["Authorization"]).toBe(`Bearer ${TOKEN}`);
  });

  it("never sends a malformed token", async () => {
    const injected = createApiClient({ baseUrl: BASE, fetch: fetchMock, token: `${TOKEN}\r\nX-Evil: 1` });
    fetchMock.mockResolvedValue(jsonResponse(principal(), 200));
    await injected.getCurrentUser();
    expect(call().headers["Authorization"]).toBeUndefined();
  });

  it("old read paths now map 401 to api_error too", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(errorBody("unauthenticated"), 401));
    expect(expectError(await client.listLinkPlans())).toMatchObject({ kind: "api_error", status: 401 });
    expect(expectError(await client.getMeta())).toMatchObject({ kind: "api_error", status: 401 });
  });

  it("probes stay public: a 401 on /ready is not a session error", async () => {
    fetchMock.mockResolvedValue(jsonResponse(errorBody("unauthenticated"), 401));
    expect(expectError(await client.getReady()).kind).toBe("http_status");
    expect(call().headers["Authorization"]).toBeUndefined();
  });
});
