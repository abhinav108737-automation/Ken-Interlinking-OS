import { describe, expect, it } from "vitest";

import type { components } from "@/lib/types.gen";
import {
  AUTONOMY_DECISIONS,
  AUTONOMY_MODES,
  BLOCK_CODES,
  MAX_REASON_CHARS,
  OPERATIONAL_STATES,
  OVERRIDE_ACTIONS,
  PRIMARY_OPERATIONAL_STATES,
  RISK_TIERS,
  VERIFICATION_PROFILES,
  blockedCodeHref,
  blockedPlanHref,
  concludesOverride,
  describeAttemptOutcome,
  describeAutonomyDecision,
  describeAutonomyMode,
  describeAutonomyModeShort,
  describeBlockCode,
  describeBlockCodeDetail,
  describeBlockInvestigation,
  describeBlockSet,
  describeCriticOutcome,
  describeDecisionConfidence,
  describeOperationalState,
  describeOverrideAction,
  describeOverrideOutcome,
  describePublishing,
  describeRiskTier,
  describeShadowDecision,
  describeVerificationProfile,
  hasOverrideErrors,
  inCooldown,
  isKnownBlockCode,
  isOpenBlock,
  isPublishDecision,
  isShadowMode,
  operationalStateOf,
  overrideErrorMessage,
  overrideOutcomeUnknown,
  parseBlockCodeFilter,
  parseDecisionFilter,
  parseModeFilter,
  planAttemptsHref,
  planDecisionsHref,
  rollUpStates,
  sortBlockCounts,
  statusesOf,
  validateOverrideInput,
} from "@/lib/autonomy";

import { autonomyDecision, publishAttempt } from "./fixtures/autonomy";

type Schemas = components["schemas"];

// Every value of each contract enum, spelled out so this test (as well as the
// Record types in lib/autonomy.ts) notices a contract change.
const MODES: Schemas["AutonomyMode"][] = ["shadow", "guarded", "autonomous"];
const DECISIONS: Schemas["AutonomyDecision"][] = [
  "block",
  "auto_publish",
  "auto_publish_with_extra_verification",
];
const CODES: Schemas["BlockCode"][] = [
  "self_link", "external_destination", "protected_page", "unpublished_destination", "broken_target",
  "stale_source_version", "duplicate_contextual_link", "prohibited_page_type",
  "policy_disallowed_destination", "invalid_canonical", "unsupported_redirect", "invalid_page_state",
  "invalid_anchor", "missing_rollback_metadata", "stale_vector",
];
const ALL_PLAN_STATUSES: Schemas["LinkPlanStatus"][] = [
  "proposed", "critiqued", "policy_checked", "autonomy_decided", "blocked", "awaiting_approval", "approved",
  "rejected", "superseded", "validated", "deployed", "verified", "failed", "rolled_back", "expired", "measured",
  "learning_captured",
];

describe("autonomy enums", () => {
  it("labels every mode, and never lets shadow read as something that happened", () => {
    expect([...AUTONOMY_MODES]).toEqual(MODES);
    for (const mode of MODES) {
      const presentation = describeAutonomyMode(mode);
      expect(presentation.known).toBe(true);
      expect(presentation.label).not.toMatch(/^Unknown/);
    }
    // The safety property: shadow says, in its own label, that nothing is published.
    expect(describeAutonomyMode("shadow").label).toMatch(/nothing is published/i);
    expect(describeAutonomyMode("shadow").description).toMatch(/counterfactual/i);
    expect(describeAutonomyMode("autonomous").description).toMatch(/without a human/i);
    expect(describeAutonomyMode("autonomous").tone).toBe("critical");
    expect(isShadowMode("shadow")).toBe(true);
    expect(isShadowMode("autonomous")).toBe(false);
    expect(describeAutonomyModeShort("guarded")).toBe("Guarded");
  });

  it("labels the three decisions and has no 'approve' among them", () => {
    expect([...AUTONOMY_DECISIONS]).toEqual(DECISIONS);
    for (const decision of DECISIONS) expect(describeAutonomyDecision(decision).known).toBe(true);
    expect(DECISIONS.join(" ")).not.toMatch(/approv/i);
    for (const decision of DECISIONS) {
      expect(describeAutonomyDecision(decision).label).not.toMatch(/approv/i);
    }
    expect(isPublishDecision("auto_publish")).toBe(true);
    expect(isPublishDecision("auto_publish_with_extra_verification")).toBe(true);
    expect(isPublishDecision("block")).toBe(false);
    expect(describeAutonomyDecision("block").tone).toBe("critical");
  });

  it("labels every deterministic block code with a meaning and something to check", () => {
    expect([...BLOCK_CODES]).toEqual(CODES);
    for (const code of CODES) {
      expect(describeBlockCode(code)).not.toMatch(/^Unknown/);
      expect(describeBlockCodeDetail(code).length).toBeGreaterThan(10);
      expect(describeBlockInvestigation(code)).not.toBeNull();
      expect(isKnownBlockCode(code)).toBe(true);
    }
    expect(describeBlockCodeDetail("missing_rollback_metadata")).toMatch(/cannot be undone|reversed/i);
    expect(describeBlockInvestigation("stale_vector")).toMatch(/qdrant/i);
  });

  it("labels risk tiers, verification profiles and critic outcomes", () => {
    expect([...RISK_TIERS]).toEqual(["low", "medium", "high"]);
    expect([...VERIFICATION_PROFILES]).toEqual(["standard", "strict"]);
    for (const tier of RISK_TIERS) expect(describeRiskTier(tier).known).toBe(true);
    for (const profile of VERIFICATION_PROFILES) {
      expect(describeVerificationProfile(profile)).not.toMatch(/^Unknown/);
    }
    for (const outcome of ["accept", "reject", "no_action"]) {
      expect(describeCriticOutcome(outcome).known).toBe(true);
    }
    // `no_action` is a separate, earlier outcome and never becomes a publish.
    expect(describeCriticOutcome("no_action").description).toMatch(/never becomes a publish/i);
  });

  it("degrades unexpected values to an explicit Unknown label", () => {
    expect(describeAutonomyMode("teleported")).toMatchObject({ label: "Unknown (teleported)", known: false });
    expect(describeAutonomyDecision("approve")).toMatchObject({ known: false });
    expect(describeBlockCode("vibes")).toBe("Unknown (vibes)");
    expect(isKnownBlockCode("vibes")).toBe(false);
    expect(describeRiskTier(" ").label).toBe("Unknown (empty)");
    expect(describeVerificationProfile("x")).toBe("Unknown (x)");
    // Inherited object keys are not labels.
    expect(describeBlockCode("toString")).toBe("Unknown (toString)");
    expect(describeAutonomyMode("constructor").known).toBe(false);
  });
});

describe("shadow labelling", () => {
  it("labels a shadow row unmistakably and names the counterfactual", () => {
    const presentation = describeShadowDecision(autonomyDecision());
    expect(presentation.shadow).toBe(true);
    expect(presentation.label).toBe("Shadow — did not happen");
    expect(presentation.description).toMatch(/nothing was published/i);
    expect(presentation.description).toMatch(/nothing on the site changed/i);
    expect(presentation.wouldHave).toBe("Auto-publish");
  });

  it("labels a shadow row even when no counterfactual decision was recorded", () => {
    const presentation = describeShadowDecision(autonomyDecision({ would_have_decision: null }));
    expect(presentation.shadow).toBe(true);
    expect(presentation.wouldHave).toBeNull();
    expect(presentation.description).toMatch(/counterfactual/i);
  });

  it("uses the ROW's mode, so a shadow record stays a counterfactual afterwards", () => {
    // The system may now be autonomous; this row was still only evaluated.
    const row = describeShadowDecision(autonomyDecision({ mode: "shadow" }));
    const acted = describeShadowDecision(autonomyDecision({ mode: "autonomous", would_have_decision: null }));
    expect(row.shadow).toBe(true);
    expect(acted.shadow).toBe(false);
    expect(acted.label).toBe("Recorded and acted on");
  });

  it("says whether anything can really be published, from the API's own flag", () => {
    expect(describePublishing({ mode: "shadow", publishing_enabled: false })).toMatchObject({
      label: "Publishing disabled",
      tone: "positive",
    });
    expect(describePublishing({ mode: "shadow", publishing_enabled: false }).description).toMatch(/counterfactual/i);
    // Guarded with publishing off is still disabled, and says why without blaming shadow.
    expect(describePublishing({ mode: "guarded", publishing_enabled: false }).description).not.toMatch(/shadow/i);
    const live = describePublishing({ mode: "autonomous", publishing_enabled: true });
    expect(live).toMatchObject({ label: "Publishing enabled", tone: "critical" });
    expect(live.description).toMatch(/without a human approving/i);
  });
});

describe("operational states", () => {
  it("has exactly the seven primary states of ADR-0018 section 3", () => {
    expect([...PRIMARY_OPERATIONAL_STATES]).toEqual([
      "proposed",
      "blocked",
      "auto_published",
      "verification_pending",
      "verified",
      "rolled_back",
      "failed",
    ]);
    for (const state of PRIMARY_OPERATIONAL_STATES) {
      expect(describeOperationalState(state).known).toBe(true);
      expect(describeOperationalState(state).label).not.toMatch(/approv/i);
    }
  });

  it("maps every contract plan status into exactly one state", () => {
    for (const status of ALL_PLAN_STATUSES) {
      expect(operationalStateOf(status)).not.toBeNull();
    }
    // No status is claimed by two states.
    const seen = new Set<string>();
    for (const state of OPERATIONAL_STATES) {
      for (const status of statusesOf(state)) {
        expect(seen.has(status)).toBe(false);
        seen.add(status);
      }
    }
    expect(seen.size).toBe(ALL_PLAN_STATUSES.length);
  });

  it("puts the approval pair under 'exception', which is not a primary state", () => {
    expect(operationalStateOf("awaiting_approval")).toBe("exception");
    expect(operationalStateOf("approved")).toBe("exception");
    expect(PRIMARY_OPERATIONAL_STATES).not.toContain("exception");
    expect(describeOperationalState("exception").description).toMatch(/not the normal path/i);
  });

  it("maps the states a monitoring operator reads most", () => {
    expect(operationalStateOf("blocked")).toBe("blocked");
    expect(operationalStateOf("validated")).toBe("auto_published");
    expect(operationalStateOf("deployed")).toBe("verification_pending");
    expect(operationalStateOf("verified")).toBe("verified");
    expect(operationalStateOf("measured")).toBe("verified");
    expect(operationalStateOf("rolled_back")).toBe("rolled_back");
    expect(operationalStateOf("failed")).toBe("failed");
    expect(operationalStateOf("autonomy_decided")).toBe("proposed");
    expect(operationalStateOf("teleported")).toBeNull();
  });

  it("rolls counts up without losing any, including statuses it does not know", () => {
    const { states, unknown, total } = rollUpStates({
      proposed: 2,
      autonomy_decided: 3,
      blocked: 4,
      deployed: 1,
      verified: 5,
      measured: 2,
      awaiting_approval: 7,
      teleported: 9,
    });
    expect(total).toBe(33);
    const byState = Object.fromEntries(states.map((entry) => [entry.state, entry.count]));
    expect(byState["proposed"]).toBe(5);
    expect(byState["blocked"]).toBe(4);
    expect(byState["verification_pending"]).toBe(1);
    expect(byState["verified"]).toBe(7);
    expect(byState["exception"]).toBe(7);
    expect(byState["auto_published"]).toBe(0);
    // The unknown count is reported, never silently dropped: the sum must tie out.
    expect(unknown).toEqual([{ key: "teleported", count: 9 }]);
    const summed = states.reduce((sum, entry) => sum + entry.count, 0) + 9;
    expect(summed).toBe(total);
  });

  it("sorts block counts in contract order with unknown codes last", () => {
    expect(
      sortBlockCounts({ stale_vector: 1, zzz: 2, self_link: 3, protected_page: 4 }).map((row) => row.key),
    ).toEqual(["self_link", "protected_page", "stale_vector", "zzz"]);
  });

  it("summarises a set of blocks, flagging one it does not know", () => {
    expect(describeBlockSet([])).toMatchObject({ known: false });
    expect(describeBlockSet([])).toHaveProperty("label", "No block recorded");
    expect(describeBlockSet(["protected_page"])).toMatchObject({ label: "Protected page", known: true });
    expect(describeBlockSet(["protected_page", "self_link"]).label).toBe("2 blocks");
    expect(describeBlockSet(["protected_page"]).description).toMatch(/never overridable/i);
    expect(describeBlockSet(["nope"]).known).toBe(false);
  });
});

describe("confidence", () => {
  it("never presents raw confidence as calibrated", () => {
    const raw = describeDecisionConfidence(autonomyDecision({ calibration_version: null }));
    expect(raw).toMatchObject({ value: "0.630", qualifier: "raw" });
    expect(raw.description).toMatch(/not a probability/i);
    const calibrated = describeDecisionConfidence(
      autonomyDecision({ calibration_version: "isotonic-1", confidence: 0.5 }),
    );
    expect(calibrated).toMatchObject({ value: "0.500", qualifier: "calibrated" });
    expect(calibrated.description).toMatch(/isotonic-1/);
    // A blank version is not a calibration.
    expect(describeDecisionConfidence(autonomyDecision({ calibration_version: "  " })).qualifier).toBe("raw");
  });
});

describe("filters and routes", () => {
  it("accepts only contract values and ignores anything else", () => {
    expect(parseDecisionFilter({ decision: "block" })).toBe("block");
    expect(parseDecisionFilter({ decision: "approve" })).toBeUndefined();
    expect(parseDecisionFilter({})).toBeUndefined();
    expect(parseModeFilter({ mode: "shadow" })).toBe("shadow");
    expect(parseModeFilter({ mode: "yolo" })).toBeUndefined();
    expect(parseBlockCodeFilter({ code: "self_link" })).toBe("self_link");
    expect(parseBlockCodeFilter({ code: "toString" })).toBeUndefined();
    expect(parseBlockCodeFilter({ code: ["stale_vector", "x"] })).toBe("stale_vector");
  });

  it("builds hrefs that escape their ids", () => {
    expect(blockedPlanHref("a/b")).toBe("/autonomy/blocked/a%2Fb");
    expect(blockedCodeHref("self_link")).toBe("/autonomy/blocked?code=self_link");
    expect(planDecisionsHref("p1")).toBe("/autonomy/decisions?plan_id=p1");
    expect(planAttemptsHref("p1")).toBe("/autonomy/attempts?plan_id=p1");
  });
});

describe("operator override", () => {
  it("names both actions and says a deterministic block is not overridable", () => {
    expect([...OVERRIDE_ACTIONS]).toEqual(["publish", "block"]);
    expect(describeOverrideAction("publish")).toBe("Publish anyway");
    expect(describeOverrideAction("x")).toBe("Unknown (x)");
  });

  it("requires an action and a reason", () => {
    expect(validateOverrideInput({ action: "publish", reason: "Editor asked.", expectedStatus: "blocked" })).toEqual({});
    const noReason = validateOverrideInput({ action: "publish", reason: "   ", expectedStatus: "blocked" });
    expect(noReason.reason).toMatch(/reason is required/i);
    expect(hasOverrideErrors(noReason)).toBe(true);
    expect(validateOverrideInput({ action: "", reason: "x", expectedStatus: "blocked" }).action).toBeDefined();
    expect(
      validateOverrideInput({ action: "publish", reason: "x".repeat(MAX_REASON_CHARS + 1), expectedStatus: "blocked" })
        .reason,
    ).toMatch(/500 characters or fewer/);
    // A status outside the frozen contract is never sent, blank or not.
    for (const expectedStatus of ["", "teleported", "toString", " blocked "]) {
      expect(
        validateOverrideInput({ action: "publish", reason: "x", expectedStatus }).expected_status,
        expectedStatus,
      ).toMatch(/out of date/i);
    }
    expect(
      validateOverrideInput({ action: "publish", reason: "x", expectedStatus: "autonomy_decided" }),
    ).toEqual({});
    expect(hasOverrideErrors({})).toBe(false);
  });

  it("reports a 200 with applied:false as a REFUSAL, not a success", () => {
    const refused = describeOverrideOutcome({
      plan_id: "p",
      action: "publish",
      applied: false,
      plan_status: "blocked",
      actor: "user:jane",
      refusal: "A deterministic block is still firing.",
      blocks: ["protected_page"],
      occurred_at: "2026-10-16T10:00:00Z",
    });
    expect(refused.status).toBe("done");
    if (refused.status !== "done") throw new Error("unreachable");
    expect(refused.applied).toBe(false);
    expect(refused.message).toMatch(/REFUSED and nothing changed/);
    expect(refused.message).toMatch(/Protected page/);
    expect(refused.message).toMatch(/never overridable/i);
  });

  it("reports an applied override with the recorded status", () => {
    const applied = describeOverrideOutcome({
      plan_id: "p",
      action: "block",
      applied: true,
      plan_status: "blocked",
      actor: "user:jane",
      refusal: null,
      blocks: [],
      occurred_at: "2026-10-16T10:00:00Z",
    });
    expect(applied.status).toBe("done");
    if (applied.status !== "done") throw new Error("unreachable");
    expect(applied.applied).toBe(true);
    expect(applied.message).toMatch(/applied/);
    expect(applied.message).toMatch(/Recorded against your account/);
  });

  // THE PHASE 6 LESSON: a 503 or a timeout must never claim nothing changed.
  it("never claims nothing changed when the outcome is unknown", () => {
    for (const error of [
      { kind: "timeout" as const, path: "/autonomy/plans/{plan_id}/override" as const, message: "timed out" },
      {
        kind: "http_status" as const,
        path: "/autonomy/plans/{plan_id}/override" as const,
        status: 503,
        message: "unavailable",
      },
      {
        kind: "api_error" as const,
        path: "/autonomy/plans/{plan_id}/override" as const,
        status: 503,
        message: "unavailable",
      },
    ]) {
      expect(overrideOutcomeUnknown(error)).toBe(true);
      const message = overrideErrorMessage(error);
      expect(message).toMatch(/UNKNOWN/);
      expect(message).not.toMatch(/nothing was changed/i);
      expect(message).toMatch(/may already have changed/i);
    }
  });

  it("explains a refusal whose outcome IS known, and says nothing changed", () => {
    const conflict = overrideErrorMessage({
      kind: "api_error",
      path: "/autonomy/plans/{plan_id}/override",
      status: 409,
      message: "conflict",
      detail: "Status was blocked, is now rejected.",
    });
    expect(overrideOutcomeUnknown({ kind: "api_error", path: "/autonomy", status: 409, message: "x" })).toBe(false);
    expect(conflict).toMatch(/no longer in the status this form was built from/i);
    expect(conflict).toMatch(/Status was blocked/);
    expect(
      overrideErrorMessage({ kind: "api_error", path: "/autonomy", status: 403, message: "x" }),
    ).toMatch(/deploy permission/);
  });

  // The operation id is re-minted ONLY after a recorded outcome, so a retry of
  // an unknown-outcome submission replays instead of applying twice.
  it("concludes an operation only on a recorded outcome", () => {
    expect(concludesOverride({ status: "idle" })).toBe(false);
    expect(concludesOverride({ status: "invalid", message: "", fieldErrors: {} })).toBe(false);
    expect(
      concludesOverride({ status: "done", message: "", applied: true, blocks: [], planStatus: "validated" }),
    ).toBe(true);
    // A refusal the API recorded is final for that id.
    expect(
      concludesOverride({ status: "done", message: "", applied: false, blocks: ["self_link"], planStatus: "blocked" }),
    ).toBe(true);
    expect(concludesOverride({ status: "error", message: "", outcomeUnknown: false, code: "conflict" })).toBe(true);
    // An unknown outcome must NOT re-mint the id.
    expect(concludesOverride({ status: "error", message: "", outcomeUnknown: true, code: "conflict" })).toBe(false);
    expect(concludesOverride({ status: "error", message: "", outcomeUnknown: false })).toBe(false);
  });
});

describe("publish attempts", () => {
  it("labels a shadow attempt as not having happened, whatever it 'succeeded' says", () => {
    const presentation = describeAttemptOutcome(publishAttempt({ mode: "shadow", succeeded: true }));
    expect(presentation.label).toBe("Shadow — did not happen");
    expect(presentation.description).toMatch(/nothing on the site changed/i);
  });

  it("distinguishes published, rolled back and failed", () => {
    expect(describeAttemptOutcome(publishAttempt())).toMatchObject({ label: "Published", tone: "positive" });
    expect(describeAttemptOutcome(publishAttempt({ rolled_back: true }))).toMatchObject({
      label: "Rolled back",
      tone: "caution",
    });
    const failed = describeAttemptOutcome(publishAttempt({ succeeded: false, failure_code: "cms_unavailable" }));
    expect(failed).toMatchObject({ label: "Failed", tone: "critical" });
    expect(failed.description).toMatch(/cms_unavailable/);
    expect(
      describeAttemptOutcome(publishAttempt({ succeeded: false, failure_code: null })).description,
    ).toMatch(/No failure code/);
  });

  it("knows whether a pair is still in its retry cool-down", () => {
    const now = Date.parse("2026-10-16T12:00:00Z");
    expect(inCooldown(publishAttempt({ cooldown_until: null }), now)).toBe(false);
    expect(inCooldown(publishAttempt({ cooldown_until: "2026-10-16T13:00:00Z" }), now)).toBe(true);
    expect(inCooldown(publishAttempt({ cooldown_until: "2026-10-16T11:00:00Z" }), now)).toBe(false);
    expect(inCooldown(publishAttempt({ cooldown_until: "not a date" }), now)).toBe(false);
  });

  it("knows an open block from one an operator already acted on", () => {
    expect(isOpenBlock({ overridden: false })).toBe(true);
    expect(isOpenBlock({ overridden: true })).toBe(false);
  });
});
