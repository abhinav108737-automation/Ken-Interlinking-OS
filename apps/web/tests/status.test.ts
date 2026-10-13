import { describe, expect, it } from "vitest";

import {
  HEALTH_STATUSES,
  READINESS_STATUSES,
  describeApiErrorKind,
  describeAutoApprove,
  describeCheckKind,
  describeHealthStatus,
  describeReadinessStatus,
  formatLatency,
  isHealthStatus,
  isReadinessStatus,
  requirementLabel,
  summarizeCheck,
  summarizeDetail,
  type HealthStatus,
  type ReadinessStatus,
} from "@/lib/status";

// Literal copies of the contract enums. Removing a value from the contract
// fails type-checking here; adding one fails type-checking in lib/status.ts
// (its mappings are exhaustive Records) and the coverage assertions below.
const CONTRACT_HEALTH: readonly HealthStatus[] = ["ok", "degraded", "unavailable", "error", "skipped"];
const CONTRACT_READINESS: readonly ReadinessStatus[] = ["ready", "degraded", "not_ready"];

describe("describeHealthStatus", () => {
  it("covers exactly the contract HealthStatus values", () => {
    expect([...HEALTH_STATUSES].sort()).toEqual([...CONTRACT_HEALTH].sort());
  });

  it.each(CONTRACT_HEALTH)("maps %s to a non-empty known label", (status) => {
    const presentation = describeHealthStatus(status);
    expect(presentation.known).toBe(true);
    expect(presentation.label.trim()).not.toBe("");
    expect(presentation.description.trim()).not.toBe("");
    expect(isHealthStatus(status)).toBe(true);
  });

  it("gives every status a distinct label", () => {
    const labels = CONTRACT_HEALTH.map((s) => describeHealthStatus(s).label);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("uses tones that match severity", () => {
    expect(describeHealthStatus("ok").tone).toBe("positive");
    expect(describeHealthStatus("degraded").tone).toBe("caution");
    expect(describeHealthStatus("unavailable").tone).toBe("critical");
    expect(describeHealthStatus("error").tone).toBe("critical");
    expect(describeHealthStatus("skipped").tone).toBe("neutral");
  });

  it("handles an unknown value without throwing", () => {
    const presentation = describeHealthStatus("exploded");
    expect(presentation.known).toBe(false);
    expect(presentation.label).toBe("Unknown (exploded)");
    expect(presentation.tone).toBe("neutral");
    expect(isHealthStatus("exploded")).toBe(false);
  });

  it("does not treat Object.prototype keys as statuses", () => {
    expect(isHealthStatus("toString")).toBe(false);
    expect(describeHealthStatus("constructor").known).toBe(false);
    expect(describeHealthStatus("").label).toBe("Unknown (empty)");
  });
});

describe("describeReadinessStatus", () => {
  it("covers exactly the contract ReadinessStatus values", () => {
    expect([...READINESS_STATUSES].sort()).toEqual([...CONTRACT_READINESS].sort());
  });

  it.each(CONTRACT_READINESS)("maps %s to a non-empty known label", (status) => {
    const presentation = describeReadinessStatus(status);
    expect(presentation.known).toBe(true);
    expect(presentation.label.trim()).not.toBe("");
    expect(isReadinessStatus(status)).toBe(true);
  });

  it("labels each verdict as text", () => {
    expect(describeReadinessStatus("ready").label).toBe("Ready");
    expect(describeReadinessStatus("degraded").label).toBe("Degraded");
    expect(describeReadinessStatus("not_ready").label).toBe("Not ready");
    expect(describeReadinessStatus("not_ready").tone).toBe("critical");
  });

  it("handles an unknown value without throwing", () => {
    const presentation = describeReadinessStatus("sideways");
    expect(presentation.known).toBe(false);
    expect(presentation.label).toBe("Unknown (sideways)");
  });
});

describe("helpers", () => {
  it("labels requirement as text", () => {
    expect(requirementLabel(true)).toBe("Required");
    expect(requirementLabel(false)).toBe("Optional");
  });

  it("labels check kinds and tolerates unknown kinds", () => {
    expect(describeCheckKind("model_server")).toBe("Model server");
    expect(describeCheckKind("database")).toBe("Database");
    expect(describeCheckKind("queue")).toBe("Unknown (queue)");
  });

  it("formats latency", () => {
    expect(formatLatency(0.2)).toBe("<1 ms");
    expect(formatLatency(12.6)).toBe("13 ms");
    expect(formatLatency(2500)).toBe("2.50 s");
    expect(formatLatency(-1)).toBe("n/a");
    expect(formatLatency(Number.NaN)).toBe("n/a");
  });

  it("summarises detail and withholds credential-like keys", () => {
    expect(summarizeDetail(undefined)).toBe("");
    expect(summarizeDetail({})).toBe("");
    const summary = summarizeDetail({ host: "localhost", port: 5433, password: "hunter2" });
    expect(summary).toBe("host: localhost; port: 5433; password: [withheld]");
    expect(summary).not.toContain("hunter2");
  });

  it("truncates long detail", () => {
    const summary = summarizeDetail({ a: 1, b: 2, c: 3, d: 4, e: 5, f: "x".repeat(200) });
    expect(summary).toBe("a: 1; b: 2; c: 3; d: 4; +2 more");
    expect(summarizeDetail({ long: "y".repeat(200) }).length).toBeLessThan(80);
  });

  it("summarises a check, treating blank errors as absent", () => {
    expect(summarizeCheck({ error: "connection refused", detail: {} })).toEqual({
      error: "connection refused",
      detail: "",
    });
    expect(summarizeCheck({ error: "  ", detail: { model: "x" } })).toEqual({
      error: null,
      detail: "model: x",
    });
  });

  it("flags auto-approve enabled as critical", () => {
    expect(describeAutoApprove(false).label).toBe("Disabled");
    expect(describeAutoApprove(true).label).toBe("Enabled");
    expect(describeAutoApprove(true).tone).toBe("critical");
  });

  it("titles every API error kind", () => {
    for (const kind of [
      "config",
      "invalid_argument",
      "network",
      "timeout",
      "api_error",
      "http_status",
      "invalid_body",
    ] as const) {
      expect(describeApiErrorKind(kind).trim()).not.toBe("");
    }
  });
});
