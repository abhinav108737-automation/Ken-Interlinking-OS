// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  endSession,
  getHealth,
  getMeta,
  getSessionUser,
  listLinkPlans,
  login,
  logout,
  requireSessionUser,
  startSession,
} from "@/lib/api";
import { REQUEST_PATH_HEADER, SESSION_COOKIE } from "@/lib/session";

import { errorBody, jsonResponse, list } from "./fixtures/inventory";
import { TOKEN, principal, sessionView } from "./fixtures/review";
import { redirectTarget } from "./redirect";
import { requestContext } from "./request-context";

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("KEN_API_BASE_URL", "http://api.test:8000");
  requestContext.headers.set("host", "127.0.0.1:3000");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

function authorizationOf(call = 0): string | undefined {
  const init = fetchMock.mock.calls[call]?.[1];
  return (init?.headers as Record<string, string> | undefined)?.["Authorization"];
}

const META = {
  identity: { service: "ken-api", version: "0.1.0", environment: "local", git_sha: null },
  python_version: "3.13.1",
  phase: 5,
  auto_approve_enabled: false,
  settings: {},
};

describe("bearer forwarding", () => {
  it("forwards the session cookie as a bearer token on every non-public call", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    fetchMock.mockResolvedValueOnce(jsonResponse(META, 200)).mockResolvedValueOnce(jsonResponse(list([]), 200));
    await getMeta();
    await listLinkPlans();
    expect(authorizationOf(0)).toBe(`Bearer ${TOKEN}`);
    expect(authorizationOf(1)).toBe(`Bearer ${TOKEN}`);
  });

  it("never sends the token to public paths", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ status: "ok", identity: META.identity, uptime_s: 1, timestamp: "t" }, 200),
    );
    await getHealth();
    fetchMock.mockResolvedValueOnce(jsonResponse(sessionView(), 200));
    await login({ username: "jane", password: "correct horse battery" });
    expect(authorizationOf(0)).toBeUndefined();
    expect(authorizationOf(1)).toBeUndefined();
  });

  it("sends no Authorization header without a cookie, or with a malformed one", async () => {
    fetchMock.mockResolvedValue(jsonResponse(META, 200));
    await getMeta();
    requestContext.cookies.set(SESSION_COOKIE, "bad token\r\nX-Injected: 1");
    await getMeta();
    expect(authorizationOf(0)).toBeUndefined();
    expect(authorizationOf(1)).toBeUndefined();
  });
});

describe("401 handling", () => {
  it("redirects to /auth/expired with the requested path when a cookie was sent", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    requestContext.headers.set(REQUEST_PATH_HEADER, "/recommendations?status=all");
    fetchMock.mockResolvedValue(jsonResponse(errorBody("unauthenticated"), 401));
    expect(await redirectTarget(() => listLinkPlans())).toBe("/auth/expired?next=%2Frecommendations%3Fstatus%3Dall");
  });

  it("redirects straight to /login when no cookie was sent", async () => {
    requestContext.headers.set(REQUEST_PATH_HEADER, "/jobs");
    fetchMock.mockResolvedValue(jsonResponse(errorBody("unauthenticated"), 401));
    expect(await redirectTarget(() => getMeta())).toBe("/login?next=%2Fjobs");
  });

  it("drops an unsafe requested path from next", async () => {
    requestContext.headers.set(REQUEST_PATH_HEADER, "//evil.example/x");
    fetchMock.mockResolvedValue(jsonResponse(errorBody("unauthenticated"), 401));
    expect(await redirectTarget(() => getMeta())).toBe("/login");
  });

  it("returns 403 as a result (no redirect)", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    fetchMock.mockResolvedValue(jsonResponse(errorBody("permission_denied"), 403));
    const result = await getMeta();
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toMatchObject({ kind: "api_error", status: 403, code: "permission_denied" });
  });

  it("getSessionUser returns the 401; requireSessionUser redirects", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    fetchMock.mockResolvedValue(jsonResponse(errorBody("unauthenticated"), 401));
    const result = await getSessionUser();
    expect(result.ok).toBe(false);
    expect(await redirectTarget(() => requireSessionUser())).toBe("/auth/expired");
  });

  it("getSessionUser returns the principal", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    fetchMock.mockResolvedValue(jsonResponse(principal("operator_admin"), 200));
    const result = await getSessionUser();
    expect(result.ok && result.data.permissions).toEqual(["read", "review", "read_audit", "read_settings"]);
    expect(String(fetchMock.mock.calls[0]![0])).toBe("http://api.test:8000/auth/me");
  });

  it("does not redirect a logout or a login 401", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    fetchMock.mockResolvedValue(jsonResponse(errorBody("unauthenticated"), 401));
    expect((await logout()).ok).toBe(false);
    expect((await login({ username: "jane", password: "wrong" })).ok).toBe(false);
  });
});

describe("startSession / endSession", () => {
  const now = Date.parse("2026-10-14T11:00:00Z");

  it("stores the token HttpOnly, SameSite=Strict, Path=/ with Max-Age = remaining TTL (loopback: not Secure)", async () => {
    expect(await startSession(sessionView({ expires_at: "2026-10-14T23:00:00Z" }), now)).toBe(true);
    expect(requestContext.writes).toEqual([
      {
        name: SESSION_COOKIE,
        value: TOKEN,
        options: { httpOnly: true, sameSite: "strict", path: "/", secure: false, maxAge: 12 * 3600 },
      },
    ]);
  });

  it("marks the cookie Secure on a non-loopback host", async () => {
    requestContext.headers.set("host", "console.example.com");
    await startSession(sessionView({ expires_at: "2026-10-14T11:30:00Z" }), now);
    expect(requestContext.writes[0]?.options).toMatchObject({ secure: true, maxAge: 1800 });
  });

  it("refuses an expired session or a malformed token", async () => {
    expect(await startSession(sessionView({ expires_at: "2026-10-14T10:00:00Z" }), now)).toBe(false);
    expect(await startSession(sessionView({ token: "bad token" }), now)).toBe(false);
    expect(requestContext.writes).toEqual([]);
  });

  it("clears the cookie with Max-Age 0 and the same attributes", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    await endSession();
    expect(requestContext.writes).toEqual([
      { name: SESSION_COOKIE, value: "", options: { httpOnly: true, sameSite: "strict", path: "/", secure: false, maxAge: 0 } },
    ]);
    expect(requestContext.cookies.has(SESSION_COOKIE)).toBe(false);
  });
});
