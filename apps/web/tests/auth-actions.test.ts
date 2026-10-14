// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loginAction, logoutAction } from "@/app/auth/actions";
import { GET as sessionExpired } from "@/app/auth/expired/route";
import { SESSION_COOKIE } from "@/lib/session";

import { errorBody, jsonResponse } from "./fixtures/inventory";
import { TOKEN, principal, sessionView } from "./fixtures/review";
import { redirectTarget } from "./redirect";
import { requestContext } from "./request-context";

const fetchMock = vi.fn<typeof fetch>();
const PASSWORD = "correct horse battery staple";

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

function form(fields: Record<string, string>): FormData {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) data.set(key, value);
  return data;
}

const IDLE = { error: null, username: "" };

describe("loginAction", () => {
  it("posts the credentials, stores the session cookie and continues to a safe next", async () => {
    fetchMock.mockResolvedValue(jsonResponse(sessionView({ expires_at: new Date(Date.now() + 3600_000).toISOString() }), 200));
    const target = await redirectTarget(() =>
      loginAction(IDLE, form({ username: " jane ", password: PASSWORD, next: "/recommendations?status=all" })),
    );
    expect(target).toBe("/recommendations?status=all");
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe("http://api.test:8000/auth/login");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ username: "jane", password: PASSWORD });
    const write = requestContext.writes[0]!;
    expect(write).toMatchObject({ name: SESSION_COOKIE, value: TOKEN });
    expect(write.options).toMatchObject({ httpOnly: true, sameSite: "strict", path: "/" });
    expect(write.options["maxAge"]).toBeGreaterThan(3500);
    expect(write.options["maxAge"]).toBeLessThanOrEqual(3600);
  });

  it("ignores an off-site next", async () => {
    fetchMock.mockResolvedValue(jsonResponse(sessionView({ expires_at: new Date(Date.now() + 3600_000).toISOString() }), 200));
    expect(await redirectTarget(() => loginAction(IDLE, form({ username: "jane", password: PASSWORD, next: "https://evil.example" })))).toBe("/");
  });

  it("validates input without calling the API", async () => {
    const state = await loginAction(IDLE, form({ username: "ja", password: PASSWORD }));
    expect(state.error).toMatch(/username/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reports a 401 generically and never echoes the password", async () => {
    fetchMock.mockResolvedValue(jsonResponse(errorBody("unauthenticated"), 401));
    const state = await loginAction(IDLE, form({ username: "jane", password: PASSWORD }));
    expect(state.error).toMatch(/not accepted/);
    expect(state.username).toBe("jane");
    expect(JSON.stringify(state)).not.toContain(PASSWORD);
    expect(requestContext.writes).toEqual([]);
  });

  it("reports an unreachable API", async () => {
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));
    const state = await loginAction(IDLE, form({ username: "jane", password: PASSWORD }));
    expect(state.error).toMatch(/could not be reached/);
  });

  it("refuses an already-expired session from the API", async () => {
    fetchMock.mockResolvedValue(jsonResponse(sessionView({ expires_at: "2000-01-01T00:00:00Z" }), 200));
    const state = await loginAction(IDLE, form({ username: "jane", password: PASSWORD }));
    expect(state.error).toMatch(/unusable session/);
    expect(requestContext.writes).toEqual([]);
  });
});

describe("logoutAction", () => {
  it("revokes the session, clears the cookie and goes to /login", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    expect(await redirectTarget(() => logoutAction())).toBe("/login");
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe("http://api.test:8000/auth/logout");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>)["Authorization"]).toBe(`Bearer ${TOKEN}`);
    expect(requestContext.writes.at(-1)).toMatchObject({ name: SESSION_COOKIE, value: "", options: { maxAge: 0 } });
  });

  it("still clears the cookie when the session had already expired", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    fetchMock.mockResolvedValue(jsonResponse(errorBody("unauthenticated"), 401));
    expect(await redirectTarget(() => logoutAction())).toBe("/login");
    expect(requestContext.cookies.has(SESSION_COOKIE)).toBe(false);
  });
});

describe("GET /auth/expired", () => {
  function request(next: string) {
    return { nextUrl: new URL(`http://127.0.0.1:3000/auth/expired?next=${encodeURIComponent(next)}`) } as Parameters<typeof sessionExpired>[0];
  }

  it("clears a cookie the API rejects and continues to /login with next", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    fetchMock.mockResolvedValue(jsonResponse(errorBody("unauthenticated"), 401));
    expect(await redirectTarget(() => sessionExpired(request("/recommendations")))).toBe(
      "/login?reason=expired&next=%2Frecommendations",
    );
    expect(requestContext.cookies.has(SESSION_COOKIE)).toBe(false);
  });

  it("keeps a cookie the API still accepts (no forced logout)", async () => {
    requestContext.cookies.set(SESSION_COOKIE, TOKEN);
    fetchMock.mockResolvedValue(jsonResponse(principal(), 200));
    expect(await redirectTarget(() => sessionExpired(request("/jobs")))).toBe("/login?next=%2Fjobs");
    expect(requestContext.writes).toEqual([]);
  });

  it("does nothing but redirect without a cookie, and sanitises next", async () => {
    expect(await redirectTarget(() => sessionExpired(request("//evil.example")))).toBe("/login");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(requestContext.writes).toEqual([]);
  });
});
