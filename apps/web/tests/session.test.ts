// @vitest-environment node
import { describe, expect, it } from "vitest";

import { isWellFormedToken } from "@/lib/api-client";
import {
  MAX_SESSION_SECONDS,
  clearedSessionCookieOptions,
  isLoopbackHost,
  loginHref,
  safeNextPath,
  sessionCookieOptions,
  sessionExpiredHref,
  sessionMaxAgeSeconds,
  shouldUseSecureCookie,
  validateLoginInput,
} from "@/lib/session";

import { TOKEN } from "./fixtures/review";

const NOW = Date.parse("2026-10-14T12:00:00Z");

describe("session cookie attributes", () => {
  it("are HttpOnly, SameSite=Strict, Path=/ with the given Max-Age and Secure flag", () => {
    expect(sessionCookieOptions(3600, true)).toEqual({ httpOnly: true, sameSite: "strict", path: "/", secure: true, maxAge: 3600 });
    expect(sessionCookieOptions(60, false)).toEqual({ httpOnly: true, sameSite: "strict", path: "/", secure: false, maxAge: 60 });
  });

  it("clear with the same attributes and Max-Age 0", () => {
    expect(clearedSessionCookieOptions(true)).toEqual({ httpOnly: true, sameSite: "strict", path: "/", secure: true, maxAge: 0 });
  });

  it("Max-Age is the session's remaining TTL in whole seconds, capped at 12 h", () => {
    expect(sessionMaxAgeSeconds("2026-10-14T13:00:00Z", NOW)).toBe(3600);
    expect(sessionMaxAgeSeconds("2026-10-14T12:00:01.900Z", NOW)).toBe(1);
    expect(sessionMaxAgeSeconds("2026-10-15T12:00:00Z", NOW)).toBe(MAX_SESSION_SECONDS);
  });

  it("refuses an expired, immediate or unparseable expiry", () => {
    expect(sessionMaxAgeSeconds("2026-10-14T11:59:59Z", NOW)).toBeNull();
    expect(sessionMaxAgeSeconds("2026-10-14T12:00:00.500Z", NOW)).toBeNull();
    expect(sessionMaxAgeSeconds("not a date", NOW)).toBeNull();
  });
});

describe("Secure flag", () => {
  it("is off only for plain-HTTP loopback DEVELOPMENT", () => {
    for (const host of ["127.0.0.1:3000", "localhost:3000", "localhost", "[::1]:3000", "app.localhost:3000"]) {
      expect(isLoopbackHost(host)).toBe(true);
      expect(shouldUseSecureCookie(host, null, true)).toBe(false);
      // QA L3: a production build stays Secure even when Host is loopback (e.g. a proxy
      // that rewrites Host without sending X-Forwarded-Proto).
      expect(shouldUseSecureCookie(host, null, false)).toBe(true);
      expect(shouldUseSecureCookie(host, null)).toBe(true);
    }
  });

  it("is on for any other host, a missing host, or a TLS-terminating proxy", () => {
    expect(shouldUseSecureCookie("console.example.com", null)).toBe(true);
    expect(shouldUseSecureCookie("10.0.0.5:3000", null)).toBe(true);
    expect(shouldUseSecureCookie(null, null)).toBe(true);
    expect(shouldUseSecureCookie("127.0.0.1:3000", "https")).toBe(true);
    expect(shouldUseSecureCookie("127.0.0.1:3000", "https, http")).toBe(true);
    expect(isLoopbackHost("127.0.0.1.evil.com")).toBe(false);
    expect(isLoopbackHost("localhost.evil.com")).toBe(false);
  });
});

describe("safeNextPath", () => {
  it("keeps a same-origin path and query", () => {
    expect(safeNextPath("/recommendations?status=all")).toBe("/recommendations?status=all");
    expect(safeNextPath("/recommendations/abc")).toBe("/recommendations/abc");
  });

  it.each([
    ["absolute URL", "https://evil.example/x"],
    ["protocol-relative", "//evil.example/x"],
    ["backslash trick", "/\\evil.example"],
    ["javascript scheme", "javascript:alert(1)"],
    ["relative path", "recommendations"],
    ["control character", "/x\n/y"],
    ["empty", ""],
    ["non-string", 42],
    ["the login page (loop)", "/login?next=/x"],
    ["an auth route (loop)", "/auth/expired"],
    ["too long", `/${"a".repeat(3000)}`],
  ])("rejects %s", (_label, raw) => {
    expect(safeNextPath(raw)).toBe("/");
    expect(safeNextPath(raw, "/fallback")).toBe("/fallback");
  });

  it("drops router-internal parameters and fragments", () => {
    expect(safeNextPath("/recommendations?_rsc=abc&status=all#x")).toBe("/recommendations?status=all");
    expect(safeNextPath("/jobs?_rsc=1")).toBe("/jobs");
  });
});

describe("sign-in hrefs", () => {
  it("carry a safe next and omit it for the home page", () => {
    expect(loginHref("/recommendations?status=all")).toBe("/login?next=%2Frecommendations%3Fstatus%3Dall");
    expect(loginHref("/")).toBe("/login");
    expect(loginHref("//evil.example")).toBe("/login");
    expect(loginHref("/jobs", "expired")).toBe("/login?reason=expired&next=%2Fjobs");
    expect(sessionExpiredHref("/audit/reviews")).toBe("/auth/expired?next=%2Faudit%2Freviews");
  });
});

describe("login input and token format", () => {
  it("validates the contract's username and password lengths", () => {
    expect(validateLoginInput("jane", "x")).toBeNull();
    expect(validateLoginInput("ja", "x")).toMatch(/username/);
    expect(validateLoginInput("j".repeat(65), "x")).toMatch(/username/);
    expect(validateLoginInput("jane", "")).toMatch(/password/);
    expect(validateLoginInput("jane", "p".repeat(257))).toMatch(/password/);
  });

  it("accepts only bearer-safe tokens", () => {
    expect(isWellFormedToken(TOKEN)).toBe(true);
    for (const bad of ["", "short", `${TOKEN}\r\nX-Evil: 1`, `${TOKEN} more`, `${TOKEN};Path=/`, "a".repeat(700), undefined, 5]) {
      expect(isWellFormedToken(bad)).toBe(false);
    }
  });
});


describe("safeNextPath after URL normalisation (QA M1)", () => {
  it("never returns a protocol-relative path built from dot segments", () => {
    for (const raw of ["/..//evil.com", "/.//evil.com", "/%2e%2e//evil.com", "/a/../..//evil.com", "/./\\evil.com"]) {
      const out = safeNextPath(raw);
      expect(out.startsWith("//")).toBe(false);
      expect(out).toBe("/");
    }
  });

  it("still keeps an ordinary normalised path", () => {
    expect(safeNextPath("/recommendations/../agents?x=1")).toBe("/agents?x=1");
  });
});
