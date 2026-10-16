// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from "vitest";

import { type ApiClient, type ApiResult, type FetchLike, createApiClient } from "@/lib/api-client";

import {
  autonomyDecision,
  autonomyHealth,
  autonomySummary,
  blockedAction,
  publishAttempt,
} from "./fixtures/autonomy";
import { errorBody, jsonResponse, list } from "./fixtures/inventory";
import { PLAN_ID } from "./fixtures/recommendations";
import { OPERATION_ID, TOKEN } from "./fixtures/review";

const BASE = "http://api.test:8000/prefix";

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

const OVERRIDE = {
  operation_id: OPERATION_ID,
  action: "publish" as const,
  reason: "The editor confirmed the destination is live.",
  expected_status: "blocked" as const,
};

const OVERRIDE_RESPONSE = {
  plan_id: PLAN_ID,
  action: "publish" as const,
  applied: false,
  plan_status: "blocked" as const,
  actor: "user:jane",
  refusal: "A deterministic block is still firing.",
  blocks: ["protected_page" as const],
  occurred_at: "2026-10-16T10:00:00Z",
};

const CASES = [
  {
    name: "getAutonomySummary",
    run: (c: ApiClient) => c.getAutonomySummary(),
    method: "GET",
    path: "/prefix/autonomy",
    response: autonomySummary(),
  },
  {
    name: "getAutonomyMode",
    run: (c: ApiClient) => c.getAutonomyMode(),
    method: "GET",
    path: "/prefix/autonomy/mode",
    response: autonomyHealth(),
  },
  {
    name: "listAutonomyDecisions",
    run: (c: ApiClient) => c.listAutonomyDecisions({ decision: "block", mode: "shadow", limit: 20 }),
    method: "GET",
    path: "/prefix/autonomy/decisions",
    response: list([autonomyDecision()]),
  },
  {
    name: "listBlockedActions",
    run: (c: ApiClient) => c.listBlockedActions({ code: "protected_page" }),
    method: "GET",
    path: "/prefix/autonomy/blocked",
    response: list([blockedAction()]),
  },
  {
    name: "listPublishAttempts",
    run: (c: ApiClient) => c.listPublishAttempts({ plan_id: PLAN_ID }),
    method: "GET",
    path: "/prefix/autonomy/attempts",
    response: list([publishAttempt()]),
  },
  {
    name: "overrideAutonomyDecision",
    run: (c: ApiClient) => c.overrideAutonomyDecision(PLAN_ID, OVERRIDE),
    method: "POST",
    path: `/prefix/autonomy/plans/${PLAN_ID}/override`,
    body: OVERRIDE,
    response: OVERRIDE_RESPONSE,
  },
] as const;

describe.each(CASES)("$name", ({ run, method, path, response, ...rest }) => {
  it("calls the contract URL with the bearer token and parses the body", async () => {
    fetchMock.mockResolvedValue(jsonResponse(response, 200));
    const result = await run(client);
    expect(result).toMatchObject({ ok: true, data: response });
    const { url, init, headers } = call();
    expect(url.pathname).toBe(path);
    expect(init.method).toBe(method);
    expect(init.redirect).toBe("manual");
    expect(headers["Authorization"]).toBe(`Bearer ${TOKEN}`);
    if (method === "POST") expect(JSON.parse(String(init.body))).toEqual("body" in rest ? rest.body : undefined);
    else expect(init.body).toBeUndefined();
  });

  it("maps 401/403 to api_error and rejects an off-contract body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("unauthenticated"), 401));
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("permission_denied"), 403));
    fetchMock.mockResolvedValueOnce(jsonResponse({ unexpected: true }, 200));
    expect(expectError(await run(client))).toMatchObject({ kind: "api_error", status: 401 });
    expect(expectError(await run(client))).toMatchObject({ kind: "api_error", status: 403, code: "permission_denied" });
    expect(expectError(await run(client)).kind).toBe("invalid_body");
  });
});

describe("autonomy queries", () => {
  it("sends only the filters it was given", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([autonomyDecision()]), 200));
    await client.listAutonomyDecisions({ decision: "block", mode: "shadow", plan_id: PLAN_ID, limit: 20, offset: 40 });
    const { url } = call();
    expect(url.searchParams.get("decision")).toBe("block");
    expect(url.searchParams.get("mode")).toBe("shadow");
    expect(url.searchParams.get("plan_id")).toBe(PLAN_ID);
    expect(url.searchParams.get("limit")).toBe("20");
    expect(url.searchParams.get("offset")).toBe("40");
  });

  it("omits a null filter entirely", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([blockedAction()]), 200));
    await client.listBlockedActions({ code: null, limit: 50, offset: 0 });
    expect(call().url.searchParams.has("code")).toBe(false);
  });
});

describe("the override mutation", () => {
  it("never sends the actor: the API derives it from the session", async () => {
    fetchMock.mockResolvedValue(jsonResponse(OVERRIDE_RESPONSE, 200));
    await client.overrideAutonomyDecision(PLAN_ID, OVERRIDE);
    const body = JSON.parse(String(call().init.body));
    expect(body).not.toHaveProperty("actor");
    expect(body.operation_id).toBe(OPERATION_ID);
    expect(body.expected_status).toBe("blocked");
  });

  it("refuses a malformed plan id without sending a request", async () => {
    const error = expectError(await client.overrideAutonomyDecision("../evil", OVERRIDE));
    expect(error.kind).toBe("invalid_argument");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("maps the contract's 404/409/422 to api_error and keeps the refusal detail", async () => {
    for (const status of [404, 409, 422]) {
      fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("conflict"), status));
      const error = expectError(await client.overrideAutonomyDecision(PLAN_ID, OVERRIDE));
      expect(error).toMatchObject({ kind: "api_error", status, code: "conflict" });
      // The API's own wording reaches the operator for a refused POST (409/422).
      if (status !== 404) expect(error.detail).toBe("Something about the request.");
    }
  });

  // The contract declares no 503 for this route, so a 503 arrives as
  // `http_status` — which is exactly what the override action treats as
  // "outcome unknown" rather than "nothing changed".
  it("surfaces an undeclared 503 as http_status, not as a success", async () => {
    fetchMock.mockResolvedValue(jsonResponse(errorBody("cms_unavailable"), 503));
    const error = expectError(await client.overrideAutonomyDecision(PLAN_ID, OVERRIDE));
    expect(error).toMatchObject({ kind: "http_status", status: 503 });
  });

  it("rejects a 200 body that is not an override response", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ...OVERRIDE_RESPONSE, blocks: "nope" }, 200));
    expect(expectError(await client.overrideAutonomyDecision(PLAN_ID, OVERRIDE)).kind).toBe("invalid_body");
  });
});
