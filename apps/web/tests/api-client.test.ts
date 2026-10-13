// @vitest-environment node
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_TIMEOUT_MS, createApiClient, type FetchLike } from "@/lib/api-client";

const IDENTITY = { service: "ken-api", version: "0.1.0", environment: "local", git_sha: null };

const READY = {
  status: "degraded",
  identity: IDENTITY,
  timestamp: "2026-10-13T12:00:00Z",
  duration_ms: 3,
  checks: [],
};

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createApiClient", () => {
  it("uses the explicit base URL and injected fetch, never the global fetch", async () => {
    const globalFetch = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", globalFetch);
    const injected = vi.fn<FetchLike>().mockResolvedValue(jsonResponse(READY, 200));

    const client = createApiClient({ baseUrl: "http://desktop.local:8123/prefix", fetch: injected });
    const result = await client.getReady();

    expect(result.ok).toBe(true);
    expect(globalFetch).not.toHaveBeenCalled();
    expect(injected).toHaveBeenCalledTimes(1);
    const [url, init] = injected.mock.calls[0]!;
    expect(String(url)).toBe("http://desktop.local:8123/prefix/ready");
    expect(init.method).toBe("GET");
    expect(init.cache).toBe("no-store");
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it("accepts a URL object as the base URL", async () => {
    const injected = vi.fn<FetchLike>().mockResolvedValue(
      jsonResponse({ status: "ok", identity: IDENTITY, uptime_s: 1, timestamp: "t" }, 200),
    );
    const client = createApiClient({ baseUrl: new URL("https://api.example.test/"), fetch: injected });
    const result = await client.getHealth();
    expect(result.ok).toBe(true);
    expect(String(injected.mock.calls[0]![0])).toBe("https://api.example.test/health");
  });

  it("falls back to the global fetch, looked up at call time", async () => {
    const client = createApiClient({ baseUrl: "http://api.test" });
    const later = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(READY, 503));
    vi.stubGlobal("fetch", later);
    const result = await client.getReady();
    expect(later).toHaveBeenCalledTimes(1);
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("unreachable");
    expect(result.status).toBe(503);
  });

  it("returns a config error for an invalid base URL without calling fetch", async () => {
    const injected = vi.fn<FetchLike>();
    for (const baseUrl of ["", "not a url", "ftp://files.test", "file:///etc/passwd"]) {
      const client = createApiClient({ baseUrl, fetch: injected });
      const result = await client.getMeta();
      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("unreachable");
      expect(result.error.kind).toBe("config");
      expect(result.error.path).toBe("/meta");
    }
    expect(injected).not.toHaveBeenCalled();
  });

  it("applies the default, client-level and per-call timeouts", async () => {
    const timeout = vi.spyOn(AbortSignal, "timeout");
    const injected = vi.fn<FetchLike>().mockImplementation(async () => jsonResponse(READY, 200));

    await createApiClient({ baseUrl: "http://api.test", fetch: injected }).getReady();
    await createApiClient({ baseUrl: "http://api.test", fetch: injected, timeoutMs: 1234 }).getReady();
    await createApiClient({ baseUrl: "http://api.test", fetch: injected, timeoutMs: 1234 }).getReady({
      timeoutMs: 50,
    });

    expect(timeout.mock.calls.map(([ms]) => ms)).toEqual([DEFAULT_TIMEOUT_MS, 1234, 50]);
  });

  it("never throws and never echoes the base URL in errors", async () => {
    const secretHost = "http://internal-only.example.test:9999";
    const cases: FetchLike[] = [
      () => Promise.reject(new TypeError(`fetch failed for ${secretHost}`)),
      () => Promise.reject(new DOMException("timed out", "TimeoutError")),
      async () => new Response("{oops", { status: 200 }),
      async () => jsonResponse({ code: "internal_error", message: secretHost }, 500),
    ];
    for (const fetchImpl of cases) {
      const result = await createApiClient({ baseUrl: secretHost, fetch: fetchImpl }).getReady();
      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("unreachable");
      expect(result.error.message).not.toContain("internal-only");
    }
  });
});

describe("api-client source portability", () => {
  const source = readFileSync(
    fileURLToPath(new URL("../lib/api-client.ts", import.meta.url)),
    "utf8",
  );

  it("contains no `process.` reference", () => {
    expect(source).not.toMatch(/\bprocess\s*\./);
  });

  it("does not use window, Node built-ins or Electron in code", () => {
    // The header comment names these things to forbid them; check code only.
    const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    expect(code).not.toMatch(/\bwindow\b/);
    expect(code).not.toMatch(/\bglobalThis\.(?:process|window|require)\b/);
    expect(code).not.toMatch(/from\s+["'](?:node:|fs|path|os|child_process)/);
    expect(code).not.toMatch(/\brequire\s*\(/);
    expect(code.toLowerCase()).not.toContain("electron");
  });
});
