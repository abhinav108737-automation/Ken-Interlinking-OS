// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getHealth, getMeta, getReady, type ReadinessResponse } from "@/lib/api";

const IDENTITY = {
  service: "ken-api",
  version: "0.1.0",
  environment: "local",
  git_sha: "abc1234",
};

function readiness(status: ReadinessResponse["status"]): ReadinessResponse {
  return {
    status,
    identity: IDENTITY,
    timestamp: "2026-10-13T12:00:00Z",
    duration_ms: 12.5,
    checks: [
      {
        name: "postgres",
        kind: "database",
        required: true,
        status: status === "not_ready" ? "unavailable" : "ok",
        latency_ms: 4,
        detail: {},
        error: status === "not_ready" ? "connection refused" : null,
      },
    ],
  };
}

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("KEN_API_BASE_URL", "http://api.test:8000");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("getReady", () => {
  it("parses a 200 ready body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(readiness("ready"), 200));
    const result = await getReady();
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("unreachable");
    expect(result.status).toBe(200);
    expect(result.data.status).toBe("ready");
    expect(result.data.checks).toHaveLength(1);
  });

  it("requests the configured URL with no-store caching and a timeout signal", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(readiness("ready"), 200));
    await getReady();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe("http://api.test:8000/ready");
    expect(init?.cache).toBe("no-store");
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("preserves a base-URL path prefix", async () => {
    vi.stubEnv("KEN_API_BASE_URL", "http://proxy.test/ken-api");
    fetchMock.mockResolvedValueOnce(jsonResponse(readiness("ready"), 200));
    await getReady();
    expect(String(fetchMock.mock.calls[0]![0])).toBe("http://proxy.test/ken-api/ready");
  });

  it("defaults to 127.0.0.1:8000 when KEN_API_BASE_URL is unset", async () => {
    vi.stubEnv("KEN_API_BASE_URL", "");
    fetchMock.mockResolvedValueOnce(jsonResponse(readiness("ready"), 200));
    await getReady();
    expect(String(fetchMock.mock.calls[0]![0])).toBe("http://127.0.0.1:8000/ready");
  });

  it("parses a 503 not_ready body as data, not as an error", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(readiness("not_ready"), 503));
    const result = await getReady();
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("unreachable");
    expect(result.status).toBe(503);
    expect(result.data.status).toBe("not_ready");
    expect(result.data.checks[0]?.error).toBe("connection refused");
  });

  it("returns a network ApiError when fetch rejects", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("fetch failed"));
    const result = await getReady();
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.kind).toBe("network");
    expect(result.error.path).toBe("/ready");
    expect(result.error.message).not.toContain("api.test");
  });

  it("returns a timeout ApiError when the request times out", async () => {
    fetchMock.mockRejectedValueOnce(new DOMException("timed out", "TimeoutError"));
    const result = await getReady();
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.kind).toBe("timeout");
  });

  it("returns an invalid_body ApiError for malformed JSON", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("{not json", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    const result = await getReady();
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.kind).toBe("invalid_body");
    expect(result.error.status).toBe(200);
  });

  it("returns an invalid_body ApiError when JSON does not match the contract", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ status: "ready", checks: "nope" }, 200));
    const result = await getReady();
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.kind).toBe("invalid_body");
  });

  it("returns an http_status ApiError for a status the contract does not define", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ code: "internal_error", message: "boom", details: [] }, 500),
    );
    const result = await getReady();
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.kind).toBe("http_status");
    expect(result.error.status).toBe(500);
    expect(result.error.code).toBe("internal_error");
  });

  it("returns a config ApiError for an invalid base URL without calling fetch", async () => {
    vi.stubEnv("KEN_API_BASE_URL", "ftp://nope");
    const result = await getReady();
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.kind).toBe("config");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("getMeta", () => {
  it("parses a 200 meta body", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          identity: IDENTITY,
          python_version: "3.13.1",
          phase: 0,
          auto_approve_enabled: false,
          settings: { model_provider: "mock" },
        },
        200,
      ),
    );
    const result = await getMeta();
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("unreachable");
    expect(result.data.phase).toBe(0);
    expect(result.data.auto_approve_enabled).toBe(false);
  });

  it("treats 503 as an error because the contract defines only 200", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ code: "service_unavailable", message: "down" }, 503),
    );
    const result = await getMeta();
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.kind).toBe("http_status");
    expect(result.error.code).toBe("service_unavailable");
  });
});

describe("getHealth", () => {
  it("parses a 200 health body", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { status: "ok", identity: IDENTITY, uptime_s: 1.5, timestamp: "2026-10-13T12:00:00Z" },
        200,
      ),
    );
    const result = await getHealth();
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("unreachable");
    expect(result.data.identity.service).toBe("ken-api");
  });

  it("returns a network ApiError when the API is down", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("fetch failed"));
    const result = await getHealth();
    expect(result.ok).toBe(false);
  });
});
