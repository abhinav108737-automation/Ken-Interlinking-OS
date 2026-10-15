// @vitest-environment node
import { describe, expect, it } from "vitest";

import {
  LINK_TYPES,
  PUBLICATION_STATES,
  RUN_STATUSES,
  buildHref,
  describeCanonicalStatus,
  describeIssueSeverity,
  describeLinkResolution,
  describeLinkType,
  describePublicationState,
  describeRunStatus,
  describeSourceType,
  firstParam,
  pageDisplayTitle,
  pageWindow,
  parseEnumParam,
  parseLimit,
  parseOffset,
  parsePositiveInt,
  parseSearchQuery,
  safeExternalHref,
  sortIssueCounts,
} from "@/lib/inventory";
import { NAV_LINKS, activeNavHref } from "@/lib/nav";

describe("enum presentations", () => {
  it("labels every contract value as known text", () => {
    for (const value of PUBLICATION_STATES) expect(describePublicationState(value).known).toBe(true);
    for (const value of RUN_STATUSES) expect(describeRunStatus(value).known).toBe(true);
    for (const value of ["resolved", "broken", "unresolved", "not_applicable"]) {
      expect(describeLinkResolution(value).known).toBe(true);
    }
    for (const value of ["info", "warning", "error"]) expect(describeIssueSeverity(value).known).toBe(true);
    expect(describeRunStatus("completed_with_errors").label).toBe("Completed with errors");
    expect(describeLinkResolution("broken").tone).toBe("critical");
    expect(describeLinkType("non_http")).toBe("Non-HTTP");
    expect(describeSourceType("file_export")).toBe("File export");
    expect(describeCanonicalStatus("self")).toMatch(/^Self/);
  });

  it("degrades unknown values to explicit text instead of failing", () => {
    expect(describeRunStatus("exploded")).toMatchObject({ label: "Unknown (exploded)", known: false });
    expect(describeLinkResolution("")).toMatchObject({ label: "Unknown (empty)", tone: "neutral" });
    expect(describeLinkType("mailto")).toBe("Unknown (mailto)");
    expect(describeSourceType("toString")).toBe("Unknown (toString)");
  });
});

describe("pageDisplayTitle", () => {
  it("falls back to the canonical URL when the title is missing or blank", () => {
    expect(pageDisplayTitle({ title: "T", canonical_url: "u" })).toBe("T");
    expect(pageDisplayTitle({ title: null, canonical_url: "u" })).toBe("u");
    expect(pageDisplayTitle({ title: "   ", canonical_url: "u" })).toBe("u");
  });
});

describe("safeExternalHref", () => {
  it.each(["https://example.com/a?b=1#c", "http://example.com"])("allows %s", (url) => {
    expect(safeExternalHref(url)).not.toBeNull();
  });

  it.each([
    "javascript:alert(1)",
    " JavaScript:alert(1)",
    "java\tscript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "mailto:a@b.c",
    "/relative/path",
    "//evil.test/x",
    "#frag",
    "",
    null,
    undefined,
  ])("refuses %j", (url) => {
    expect(safeExternalHref(url)).toBeNull();
  });
});

describe("query-string parsing", () => {
  const params = {
    q: "  widgets  ",
    many: ["first", "second"],
    blank: "   ",
    offset: "100",
    limit: "500",
    version: "3",
    state: "draft",
  };

  it("takes the first trimmed value and treats blanks as absent", () => {
    expect(firstParam(params, "q")).toBe("widgets");
    expect(firstParam(params, "many")).toBe("first");
    expect(firstParam(params, "blank")).toBeUndefined();
    expect(firstParam(params, "missing")).toBeUndefined();
  });

  it("clamps offset and limit to the contract bounds", () => {
    expect(parseOffset(params)).toBe(100);
    expect(parseOffset({ offset: "-5" })).toBe(0);
    expect(parseOffset({ offset: "1e3" })).toBe(0);
    expect(parseOffset({ offset: "999999999" })).toBe(10_000_000);
    expect(parseLimit(params)).toBe(200);
    expect(parseLimit({ limit: "0" })).toBe(50);
    expect(parseLimit({})).toBe(50);
    expect(parseOffset({ links_offset: "20" }, "links_offset")).toBe(20);
  });

  it("parses positive integers, enums and the search query", () => {
    expect(parsePositiveInt(params, "version")).toBe(3);
    expect(parsePositiveInt({ version: "0" }, "version")).toBeUndefined();
    expect(parsePositiveInt({ version: "2.5" }, "version")).toBeUndefined();
    expect(parseEnumParam(params, "state", PUBLICATION_STATES)).toBe("draft");
    expect(parseEnumParam({ t: "<script>" }, "t", LINK_TYPES)).toBeUndefined();
    expect(parseSearchQuery({ q: "x".repeat(300) })).toHaveLength(200);
  });
});

describe("buildHref", () => {
  it("URL-encodes values and drops empty ones", () => {
    expect(buildHref("/inventory", {})).toBe("/inventory");
    expect(buildHref("/inventory", { q: "a&b c", offset: 50, publication_state: undefined, x: "" })).toBe(
      "/inventory?q=a%26b+c&offset=50",
    );
  });
});

describe("pageWindow", () => {
  it("computes the visible range and neighbouring offsets", () => {
    expect(pageWindow({ total: 120, limit: 50, offset: 50, has_more: true }, 50)).toEqual({
      first: 51,
      last: 100,
      total: 120,
      prevOffset: 0,
      nextOffset: 100,
    });
    expect(pageWindow({ total: 3, limit: 50, offset: 0, has_more: false }, 3)).toMatchObject({
      first: 1,
      last: 3,
      prevOffset: null,
      nextOffset: null,
    });
    expect(pageWindow({ total: 3, limit: 50, offset: 500, has_more: false }, 0)).toMatchObject({
      first: 0,
      prevOffset: 450,
    });
  });
});

describe("sortIssueCounts", () => {
  it("orders by count descending, then code", () => {
    expect(sortIssueCounts({ b: 1, a: 1, c: 5 })).toEqual([
      { code: "c", count: 5 },
      { code: "a", count: 1 },
      { code: "b", count: 1 },
    ]);
  });
});

describe("activeNavHref", () => {
  it("picks the longest matching section", () => {
    expect(activeNavHref("/")).toBe("/");
    expect(activeNavHref("/system")).toBe("/system");
    expect(activeNavHref("/inventory")).toBe("/inventory");
    expect(activeNavHref("/inventory/pages/abc")).toBe("/inventory");
    expect(activeNavHref("/inventory/runs")).toBe("/inventory/runs");
    expect(activeNavHref("/inventory/runs/xyz")).toBe("/inventory/runs");
    expect(activeNavHref("/agents")).toBe("/agents");
    expect(activeNavHref("/agents/abc")).toBe("/agents");
    expect(activeNavHref("/inventoryx")).toBeNull();
    expect(activeNavHref(null)).toBeNull();
  });

  it("keeps System in the navigation", () => {
    expect(NAV_LINKS.map((link) => link.label)).toEqual([
      "Dashboard", "Inventory", "Runs", "Intelligence", "Recommendations", "Agents", "Deployments", "Jobs", "Audit", "Settings", "System",
    ]);
  });
});
