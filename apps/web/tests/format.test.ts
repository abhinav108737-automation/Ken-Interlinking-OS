// @vitest-environment node
import { describe, expect, it } from "vitest";

import {
  NONE,
  WITHHELD,
  durationBetween,
  formatConfigValue,
  formatCount,
  formatDateTime,
  formatDuration,
  shortHash,
  toDisplayEntries,
} from "@/lib/format";

describe("formatCount", () => {
  it("groups thousands and handles absent values", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(1234567)).toBe("1,234,567");
    expect(formatCount(null)).toBe(NONE);
    expect(formatCount(Number.NaN)).toBe(NONE);
  });
});

describe("formatDateTime", () => {
  it("renders UTC deterministically regardless of host time zone", () => {
    expect(formatDateTime("2026-10-13T12:00:05Z")).toBe("2026-10-13 12:00:05 UTC");
    expect(formatDateTime("2026-10-13T12:00:05+05:30")).toBe("2026-10-13 06:30:05 UTC");
  });

  it("shows absent values as a dash and unparseable values verbatim", () => {
    expect(formatDateTime(null)).toBe(NONE);
    expect(formatDateTime(undefined)).toBe(NONE);
    expect(formatDateTime("  ")).toBe(NONE);
    expect(formatDateTime("yesterday")).toBe("yesterday");
  });
});

describe("formatDuration / durationBetween", () => {
  it.each([
    [0, "0 ms"],
    [850.4, "850 ms"],
    [12_345, "12.3 s"],
    [185_000, "3 min 05 s"],
    [7_440_000, "2 h 04 min"],
    [-1, NONE],
    [Number.POSITIVE_INFINITY, NONE],
  ])("formats %s ms as %s", (ms, expected) => {
    expect(formatDuration(ms)).toBe(expected);
  });

  it("computes durations and refuses missing or reversed ends", () => {
    expect(durationBetween("2026-10-13T10:00:00Z", "2026-10-13T10:02:05Z")).toBe(125_000);
    expect(durationBetween("2026-10-13T10:00:00Z", null)).toBeNull();
    expect(durationBetween("2026-10-13T10:00:00Z", "2026-10-13T09:00:00Z")).toBeNull();
    expect(durationBetween("bad", "2026-10-13T09:00:00Z")).toBeNull();
  });
});

describe("shortHash", () => {
  it("drops an algorithm prefix and keeps 12 characters", () => {
    expect(shortHash("sha256:0123456789abcdef0123")).toBe("0123456789ab");
    expect(shortHash("abcdef")).toBe("abcdef");
    expect(shortHash("0123456789abcdef", 8)).toBe("01234567");
  });
});

describe("formatConfigValue / toDisplayEntries", () => {
  it("renders strings verbatim and everything else as JSON", () => {
    expect(formatConfigValue("https://x.test/")).toBe("https://x.test/");
    expect(formatConfigValue(500)).toBe("500");
    expect(formatConfigValue(true)).toBe("true");
    expect(formatConfigValue(null)).toBe("null");
    expect(formatConfigValue(["a", "b"])).toBe('["a","b"]');
    expect(formatConfigValue(undefined)).toBe("undefined");
  });

  it("withholds credential-like keys at any depth", () => {
    expect(formatConfigValue({ depth: 3, password: "p", auth: { api_key: "k" } })).toBe(
      `{"depth":3,"password":"${WITHHELD}","auth":{"api_key":"${WITHHELD}"}}`,
    );
  });

  it("never throws on unserialisable values", () => {
    const cyclic: Record<string, unknown> = {};
    cyclic["self"] = cyclic;
    expect(formatConfigValue(cyclic)).toBe("[unrepresentable value]");
    expect(formatConfigValue(10n)).toBe("[unrepresentable value]");
  });

  it("sorts entries and withholds sensitive top-level keys", () => {
    expect(toDisplayEntries({ max_pages: 500, base_url: "https://x.test/", cms_token: "t" })).toEqual([
      { key: "base_url", value: "https://x.test/", withheld: false },
      { key: "cms_token", value: WITHHELD, withheld: true },
      { key: "max_pages", value: "500", withheld: false },
    ]);
  });
});
