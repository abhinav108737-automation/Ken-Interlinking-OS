// @vitest-environment node
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import type { ApiError } from "@/lib/api-client";
import { EVENT_NAMES, POLICY_KINDS, describeEventName, describePolicyKind, formatPayload, parseEventNameParam } from "@/lib/audit";
import { PERMISSIONS, ROLES, can, describePermission, describeRole, permissionsOf } from "@/lib/auth";
import { NAV_LINKS, activeNavHref, navLinksFor } from "@/lib/nav";
import {
  BULK_ACTIONS,
  FEEDBACK_REASONS,
  REASONS_BY_ACTION,
  REVIEW_ACTIONS,
  availableActions,
  describeBulkAction,
  describeBulkItemError,
  describeFeedbackReason,
  describeReviewAction,
  reviewErrorMessage,
  sliceCodePoints,
  spanOfWords,
  tallyBulk,
  tokenizeWords,
  validateBulkInput,
  validateReasonsAndNote,
  validateSpan,
  wordRangeOfSpan,
  wordsOf,
} from "@/lib/review";

import { PLAN_ID } from "./fixtures/recommendations";
import { bulkResult, principal } from "./fixtures/review";

// --- Contract enums, read from the frozen OpenAPI document ------------------

const openapi = JSON.parse(
  readFileSync(fileURLToPath(new URL("../../../contracts/openapi/openapi.json", import.meta.url)), "utf8"),
) as { components: { schemas: Record<string, { enum?: string[]; properties?: Record<string, { enum?: string[] }> }> } };

function contractEnum(name: string): string[] {
  const values = openapi.components.schemas[name]?.enum;
  if (values === undefined) throw new Error(`no enum ${name}`);
  return values;
}

describe("label completeness against the contract", () => {
  it.each([
    ["FeedbackReason", FEEDBACK_REASONS, describeFeedbackReason],
    ["ReviewAction", REVIEW_ACTIONS, describeReviewAction],
    ["BulkAction", BULK_ACTIONS, describeBulkAction],
    ["Role", ROLES, describeRole],
    ["Permission", PERMISSIONS, describePermission],
    ["EventName", EVENT_NAMES, describeEventName],
  ] as const)("labels every %s value, distinctly", (name, values, describe) => {
    expect([...values].sort()).toEqual([...contractEnum(name)].sort());
    const labels = values.map((value) => describe(value));
    expect(labels.every((label) => !label.startsWith("Unknown"))).toBe(true);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("labels every policy kind", () => {
    const kinds = openapi.components.schemas["PolicyDocument"]?.properties?.["kind"]?.enum ?? [];
    expect([...POLICY_KINDS].sort()).toEqual([...kinds].sort());
    expect(POLICY_KINDS.map(describePolicyKind).every((label) => !label.startsWith("Unknown"))).toBe(true);
  });

  it("degrades unknown values to an explicit label", () => {
    for (const describe of [describeFeedbackReason, describeReviewAction, describeBulkAction, describeRole, describePermission, describeEventName, describePolicyKind]) {
      expect(describe("from_the_future")).toBe("Unknown (from_the_future)");
      expect(describe("")).toBe("Unknown (empty)");
    }
  });
});

// --- Rules mirrored from ken_contracts.review --------------------------------

describe("REASONS_BY_ACTION", () => {
  it("matches the contract exactly", () => {
    const all = contractEnum("FeedbackReason");
    expect([...REASONS_BY_ACTION.approve].sort()).toEqual(["good_as_is", "other"]);
    expect([...REASONS_BY_ACTION.reject].sort()).toEqual(all.filter((r) => r !== "good_as_is").sort());
    expect([...REASONS_BY_ACTION.edit_anchor].sort()).toEqual(
      ["misleading_anchor", "unnatural_anchor", "anchor_too_generic", "wrong_placement", "over_linking", "other"].sort(),
    );
    expect([...REASONS_BY_ACTION.replace_target].sort()).toEqual(
      [
        "irrelevant_destination",
        "better_destination_exists",
        "reader_journey_mismatch",
        "duplicate_of_existing_link",
        "sensitive_or_protected",
        "other",
      ].sort(),
    );
  });
});

describe("validateReasonsAndNote", () => {
  it("approve: reasons optional, only good_as_is / other", () => {
    expect(validateReasonsAndNote("approve", { reasons: [], note: "" })).toEqual({});
    expect(validateReasonsAndNote("approve", { reasons: ["good_as_is"], note: "" })).toEqual({});
    expect(validateReasonsAndNote("approve", { reasons: ["irrelevant_destination"], note: "" }).reasons).toMatch(/does not apply/);
  });

  it("reject: at least one reason, never good_as_is", () => {
    expect(validateReasonsAndNote("reject", { reasons: [], note: "" }).reasons).toMatch(/at least one/);
    expect(validateReasonsAndNote("reject", { reasons: ["good_as_is"], note: "" }).reasons).toMatch(/does not apply/);
    expect(validateReasonsAndNote("reject", { reasons: ["over_linking"], note: "" })).toEqual({});
  });

  it("edits: reasons from their own sets", () => {
    expect(validateReasonsAndNote("edit_anchor", { reasons: ["irrelevant_destination"], note: "" }).reasons).toBeDefined();
    expect(validateReasonsAndNote("edit_anchor", { reasons: ["wrong_placement"], note: "" })).toEqual({});
    expect(validateReasonsAndNote("replace_target", { reasons: ["unnatural_anchor"], note: "" }).reasons).toBeDefined();
    expect(validateReasonsAndNote("replace_target", { reasons: [], note: "" }).reasons).toMatch(/at least one/);
  });

  it("'other' requires a non-blank note, for every action", () => {
    for (const action of REVIEW_ACTIONS) {
      expect(validateReasonsAndNote(action, { reasons: ["other"], note: "  " }).note).toMatch(/Other/);
      expect(validateReasonsAndNote(action, { reasons: ["other"], note: "Because." })).toEqual({});
    }
  });

  it("rejects duplicates, more than five reasons, long notes and NUL", () => {
    expect(validateReasonsAndNote("reject", { reasons: ["over_linking", "over_linking"], note: "" }).reasons).toMatch(/once/);
    expect(validateReasonsAndNote("reject", { reasons: REASONS_BY_ACTION.reject.slice(0, 6), note: "x" }).reasons).toMatch(/at most 5/);
    expect(validateReasonsAndNote("approve", { reasons: [], note: "x".repeat(501) }).note).toMatch(/500/);
    expect(validateReasonsAndNote("approve", { reasons: [], note: "a\u0000b" }).note).toMatch(/invalid/);
  });
});

describe("validateBulkInput", () => {
  it("requires 1–50 unique plan UUIDs", () => {
    expect(validateBulkInput("approve", [], { reasons: [], note: "" }).plans).toMatch(/at least one/);
    const many = Array.from({ length: 51 }, (_, i) => `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`);
    expect(validateBulkInput("approve", many, { reasons: [], note: "" }).plans).toMatch(/at most 50/);
    expect(validateBulkInput("approve", many.slice(0, 50), { reasons: [], note: "" })).toEqual({});
    expect(validateBulkInput("approve", [PLAN_ID, PLAN_ID], { reasons: [], note: "" }).plans).toMatch(/twice/);
    expect(validateBulkInput("approve", ["../x"], { reasons: [], note: "" }).plans).toMatch(/not valid/);
  });

  it("bulk reject needs a reject reason; bulk approve only approve reasons", () => {
    expect(validateBulkInput("reject", [PLAN_ID], { reasons: [], note: "" }).reasons).toMatch(/at least one/);
    expect(validateBulkInput("reject", [PLAN_ID], { reasons: ["good_as_is"], note: "" }).reasons).toBeDefined();
    expect(validateBulkInput("reject", [PLAN_ID], { reasons: ["over_linking"], note: "" })).toEqual({});
    expect(validateBulkInput("approve", [PLAN_ID], { reasons: ["over_linking"], note: "" }).reasons).toBeDefined();
  });
});

describe("availableActions", () => {
  it("follows the contract's status sets", () => {
    for (const status of ["proposed", "critiqued", "policy_checked", "awaiting_approval"]) {
      expect(availableActions(status)).toEqual({ approve: true, reject: true, edit: true });
    }
    expect(availableActions("approved")).toEqual({ approve: false, reject: true, edit: true });
    for (const status of ["rejected", "superseded", "deployed", "verified", "expired", "nonsense"]) {
      expect(availableActions(status)).toEqual({ approve: false, reject: false, edit: false });
    }
  });
});

// --- Span picker ---------------------------------------------------------------

describe("span picker offsets", () => {
  const TEXT = "The global lithium-ion battery market is growing.";

  it("tokenises words with code-point offsets and keeps every character", () => {
    const pieces = tokenizeWords(TEXT);
    expect(pieces.map((piece) => piece.text).join("")).toBe(TEXT);
    const words = wordsOf(pieces);
    expect(words.map((word) => word.text)).toEqual(["The", "global", "lithium-ion", "battery", "market", "is", "growing"]);
    expect(words[2]).toMatchObject({ start: 11, end: 22 });
    const span = spanOfWords(words, 4, 2)!;
    expect(span).toEqual({ start: 11, end: 37 });
    expect(sliceCodePoints(TEXT, span.start, span.end)).toBe("lithium-ion battery market");
    expect(wordRangeOfSpan(words, 11, 37)).toEqual({ first: 2, last: 4 });
    expect(wordRangeOfSpan(words, 12, 37)).toBeNull();
  });

  it("counts code points, not UTF-16 units, after astral characters and marks", () => {
    const text = "🔋🔋 Ünïcode café — 日本 市場 report";
    const words = wordsOf(tokenizeWords(text));
    expect(words.map((word) => word.text)).toEqual(["Ünïcode", "café", "日本", "市場", "report"]);
    const cafe = words[1]!;
    // Two emoji are 4 UTF-16 units but 2 code points.
    expect(cafe.start).toBe(Array.from("🔋🔋 Ünïcode ").length);
    expect(sliceCodePoints(text, cafe.start, cafe.end)).toBe("café");
    const span = spanOfWords(words, 2, 3)!;
    expect(sliceCodePoints(text, span.start, span.end)).toBe("日本 市場");
    expect(text.slice(span.start, span.end)).not.toBe("日本 市場");
  });

  it("keeps combining marks inside a word", () => {
    const text = "résumé here";
    const [first] = wordsOf(tokenizeWords(text));
    expect(first?.text).toBe("résumé");
    expect(first?.end).toBe(8);
  });

  it("validates spans and preserves markup-like text verbatim", () => {
    expect(validateSpan(TEXT, 11, 37)).toBeNull();
    expect(validateSpan(TEXT, 10, 37)).toMatch(/whitespace/);
    expect(validateSpan(TEXT, 10, 11)).toMatch(/only whitespace/);
    expect(validateSpan(TEXT, 20, 20)).toMatch(/end must come after/);
    expect(validateSpan(TEXT, 0, 999)).toMatch(/inside the block/);
    const markup = "Read <script>alert(1)</script> now";
    expect(sliceCodePoints(markup, 5, 30)).toBe("<script>alert(1)</script>");
  });
});

// --- Errors ------------------------------------------------------------------

function apiError(overrides: Partial<ApiError>): ApiError {
  return { kind: "api_error", path: "/recommendations/{plan_id}/approve", message: "m", ...overrides };
}

describe("reviewErrorMessage", () => {
  it("distinguishes stale, conflict, permission, validation and transport failures", () => {
    expect(reviewErrorMessage(apiError({ status: 409, code: "stale_page_version" }))).toMatch(/source page has changed/);
    expect(reviewErrorMessage(apiError({ status: 409, code: "conflict", detail: "Status mismatch." }))).toMatch(
      /changed since you loaded it.*The API said: Status mismatch\./,
    );
    expect(reviewErrorMessage(apiError({ status: 403, code: "permission_denied" }))).toMatch(/role does not permit/);
    expect(reviewErrorMessage(apiError({ status: 422, detail: "Span overlaps a link." }))).toBe(
      "The API rejected the input. Nothing was changed. The API said: Span overlaps a link.",
    );
    expect(reviewErrorMessage(apiError({ status: 404 }))).toMatch(/no longer exists/);
    expect(reviewErrorMessage(apiError({ kind: "timeout" }))).toMatch(/may or may not/);
    expect(reviewErrorMessage(apiError({ kind: "network" }))).toMatch(/could not be reached/);
  });

  it("labels bulk item failures, unknown codes explicitly", () => {
    expect(describeBulkItemError("stale_page_version")).toMatch(/stale/);
    expect(describeBulkItemError("conflict")).toMatch(/Not awaiting approval/);
    expect(describeBulkItemError("brand_new")).toBe("Unknown (brand_new)");
    expect(describeBulkItemError(null)).toMatch(/no reason/);
    expect(tallyBulk(bulkResult())).toEqual({ succeeded: 1, failed: 1 });
  });
});

// --- Roles, navigation, audit text ---------------------------------------------

describe("role-aware navigation", () => {
  it("hides Audit and Settings without their permissions", () => {
    const labels = (role: "viewer" | "reviewer" | "operator_admin") =>
      navLinksFor(permissionsOf(principal(role))).map((link) => link.label);
    expect(labels("viewer")).not.toContain("Audit");
    expect(labels("viewer")).not.toContain("Settings");
    expect(labels("reviewer")).not.toContain("Audit");
    expect(labels("operator_admin")).toEqual(NAV_LINKS.map((link) => link.label));
    expect(navLinksFor(permissionsOf(null)).map((link) => link.label)).toContain("System");
  });

  it("checks permissions only as returned by the API", () => {
    expect(can(permissionsOf(principal("viewer")), "review")).toBe(false);
    expect(can(permissionsOf(principal("reviewer")), "review")).toBe(true);
    expect(can(permissionsOf(principal("viewer", { permissions: ["read", "review"] })), "review")).toBe(true);
    expect(can(permissionsOf(null), "read")).toBe(false);
  });

  it("marks the audit and settings sections active for their sub-pages", () => {
    expect(activeNavHref("/audit/events")).toBe("/audit/reviews");
    expect(activeNavHref("/audit/reviews")).toBe("/audit/reviews");
    expect(activeNavHref("/settings/policies")).toBe("/settings/policies");
  });
});

describe("audit text", () => {
  it("renders payloads as compact JSON, withholding credential-like keys at any depth", () => {
    expect(formatPayload({ plan_id: "p", nested: { api_key: "s3cret", ok: 1 }, note: "<script>" })).toBe(
      '{"plan_id":"p","nested":{"api_key":"[withheld]","ok":1},"note":"<script>"}',
    );
  });

  it("accepts only contract event names as a filter", () => {
    expect(parseEventNameParam({ event_name: "link.approved" })).toBe("link.approved");
    expect(parseEventNameParam({ event_name: "link.approved; drop" })).toBeUndefined();
    expect(parseEventNameParam({})).toBeUndefined();
  });
});
