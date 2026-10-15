// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from "vitest";

import { type ApiClient, type ApiResult, type FetchLike, createApiClient } from "@/lib/api-client";

import { DEPLOYMENT_ID, deploymentDetail, deploymentView, dryRunResult } from "./fixtures/deployments";
import { PAGE_ID, errorBody, jsonResponse, list } from "./fixtures/inventory";
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

const DEPLOY = { operation_id: OPERATION_ID, plan_ids: [PLAN_ID], note: "" };
const ROLLBACK = { operation_id: OPERATION_ID, reason: "other" as const, note: "Legal asked.", plan_ids: null };

const CASES = [
  { name: "dryRunDeployment", run: (c: ApiClient) => c.dryRunDeployment(DEPLOY), method: "POST", path: "/prefix/deployments/dry-run", body: DEPLOY, response: dryRunResult() },
  { name: "createDeployment", run: (c: ApiClient) => c.createDeployment(DEPLOY), method: "POST", path: "/prefix/deployments", body: DEPLOY, response: deploymentDetail() },
  { name: "verifyDeployment", run: (c: ApiClient) => c.verifyDeployment(DEPLOYMENT_ID), method: "POST", path: `/prefix/deployments/${DEPLOYMENT_ID}/verify`, body: {}, response: deploymentDetail() },
  { name: "rollbackDeployment", run: (c: ApiClient) => c.rollbackDeployment(DEPLOYMENT_ID, ROLLBACK), method: "POST", path: `/prefix/deployments/${DEPLOYMENT_ID}/rollback`, body: ROLLBACK, response: deploymentDetail({ status: "rolled_back", rollback_reason: "other", rollback_note: "Legal asked." }) },
  { name: "listDeployments", run: (c: ApiClient) => c.listDeployments({ status: "verified", limit: 20 }), method: "GET", path: "/prefix/deployments", response: list([deploymentView()]) },
  { name: "getDeployment", run: (c: ApiClient) => c.getDeployment(DEPLOYMENT_ID), method: "GET", path: `/prefix/deployments/${DEPLOYMENT_ID}`, response: deploymentDetail() },
  { name: "listPlanDeployments", run: (c: ApiClient) => c.listPlanDeployments(PLAN_ID), method: "GET", path: `/prefix/recommendations/${PLAN_ID}/deployments`, response: [deploymentView()] },
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

describe("deployment errors", () => {
  it("carries a 409's issue codes and locations, never messages", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          code: "conflict",
          message: "The deployment was refused.",
          request_id: "r1",
          details: [
            { code: "already_linked", location: PLAN_ID, message: "Already linked." },
            { code: "deployment_in_progress", location: null, message: "Busy." },
            { code: "<script>", location: "x", message: "dropped" },
            { code: "plan_stale", location: "has spaces/and slashes", message: "kept, location dropped" },
          ],
        },
        409,
      ),
    );
    const error = expectError(await client.createDeployment(DEPLOY));
    expect(error).toMatchObject({ kind: "api_error", status: 409, code: "conflict", detail: "The deployment was refused.", requestId: "r1" });
    expect(error.details).toEqual([
      { code: "already_linked", location: PLAN_ID },
      { code: "deployment_in_progress", location: null },
      { code: "plan_stale", location: null },
    ]);
  });

  it("declares 409 cms_version_conflict, 503 cms_unavailable, 404 and 422", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("cms_version_conflict"), 409));
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("cms_unavailable"), 503));
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("not_found"), 404));
    fetchMock.mockResolvedValueOnce(jsonResponse({ ...errorBody("validation_failed"), details: [{ code: "missing", location: "body.reason", message: "m" }] }, 422));
    expect(expectError(await client.createDeployment(DEPLOY))).toMatchObject({ kind: "api_error", status: 409, code: "cms_version_conflict" });
    expect(expectError(await client.dryRunDeployment(DEPLOY))).toMatchObject({ kind: "api_error", status: 503, code: "cms_unavailable" });
    expect(expectError(await client.verifyDeployment(DEPLOYMENT_ID))).toMatchObject({ kind: "api_error", status: 404 });
    expect(expectError(await client.rollbackDeployment(DEPLOYMENT_ID, ROLLBACK))).toMatchObject({
      kind: "api_error",
      status: 422,
      details: [{ code: "missing", location: "body.reason" }],
    });
  });

  it("an empty details list adds no details; GET errors never carry details", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("conflict"), 409));
    expect(expectError(await client.createDeployment(DEPLOY)).details).toBeUndefined();
    fetchMock.mockResolvedValueOnce(jsonResponse({ ...errorBody("validation_failed"), details: [{ code: "x", location: null, message: "m" }] }, 422));
    expect(expectError(await client.listDeployments()).details).toBeUndefined();
  });

  it("refuses a malformed id without sending", async () => {
    for (const bad of ["../x", "a/b", "%2e%2e", ""]) {
      expect(expectError(await client.getDeployment(bad)).kind).toBe("invalid_argument");
      expect(expectError(await client.verifyDeployment(bad)).kind).toBe("invalid_argument");
      expect(expectError(await client.rollbackDeployment(bad, ROLLBACK)).kind).toBe("invalid_argument");
      expect(expectError(await client.listPlanDeployments(bad)).kind).toBe("invalid_argument");
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("encodes history filters", async () => {
    fetchMock.mockResolvedValue(jsonResponse(list([]), 200));
    await client.listDeployments({ status: "rolled_back", source_page_id: PAGE_ID, limit: 20, offset: 40 });
    expect(Object.fromEntries(call().url.searchParams)).toEqual({ status: "rolled_back", source_page_id: PAGE_ID, limit: "20", offset: "40" });
    await client.listDeployments({ status: null, source_page_id: null });
    expect(call(1).url.search).toBe("");
  });

  it("treats a deploy timeout as a timeout (the change may have happened)", async () => {
    fetchMock.mockRejectedValue(new DOMException("timed out", "TimeoutError"));
    expect(expectError(await client.createDeployment(DEPLOY)).kind).toBe("timeout");
  });
});
