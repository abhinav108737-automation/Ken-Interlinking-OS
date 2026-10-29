import { describe, expect, it } from "vitest";

import type { components } from "@/lib/types.gen";
import {
  GATE_RESULTS,
  MAX_NOTE_CHARS,
  PROPOSAL_REJECTION_REASONS,
  PROPOSAL_STATUSES,
  SKILL_STATUSES,
  advanceErrorMessage,
  advanceOutcomeUnknown,
  belowSampleFloor,
  concludesAdvance,
  describeAdvanceOutcome,
  describeCorpusBanner,
  describeEvalSuite,
  describeEvaluationMode,
  describeGateReasonCode,
  describeGateResult,
  describeProposalKind,
  describeProposalRejectionReason,
  describeProposalStatus,
  describePromotionBlock,
  describePromotionRefusal,
  describeSkillSource,
  describeSkillStatus,
  describeTargetKind,
  hasAdvanceErrors,
  parseGateResult,
  parseProposalKind,
  parseProposalStatus,
  parseSkillStatus,
  proposalHref,
  reasonRequiredFor,
  skillHref,
  validateAdvanceInput,
} from "@/lib/learning";

type Schemas = components["schemas"];

const PROPOSAL: Schemas["ProposalStatus"][] = [
  "proposed", "offline_test", "shadow", "canary", "experiment", "promoted", "rejected", "rolled_back", "withdrawn",
];
const SKILL: Schemas["SkillStatus"][] = ["draft", "offline_test", "promoted", "rejected", "retired"];
const REFUSALS: Schemas["PromotionRefusal"][] = [
  "demonstration_mode", "not_a_human", "permission_denied", "transition_not_allowed", "required_stage_missing",
  "evaluation_missing", "evaluation_not_found", "evaluation_not_passed", "evaluation_foreign",
  "fingerprint_mismatch", "dataset_changed", "evaluation_too_old", "sample_too_small", "behavioural_block",
  "frozen_field", "reason_required", "replayed_with_different_body", "promoter_guard_missing",
];
const GATE_CODES: Schemas["GateReasonCode"][] = [
  "hard_fail_suite", "must_not_violation", "below_min_pass_rate", "regression_vs_baseline", "suite_not_run",
  "sample_too_small", "behavioural_signal_negative", "evidence_not_reproducible", "frozen_field_touched",
  "baseline_missing",
];

function banner(overrides: Partial<Schemas["CorpusBanner"]> = {}): Schemas["CorpusBanner"] {
  return {
    demonstration_mode: true,
    note: "This corpus is 65 synthetic pages and 48 plans. No proposal on this data may be promoted.",
    pages_total: 65,
    plans_total: 48,
    review_actions_total: 12,
    min_sample_count: 30,
    ...overrides,
  };
}

describe("corpus banner (ADR-0016 section 16.1)", () => {
  it("names the corpus size and that nothing here may be promoted", () => {
    const presentation = describeCorpusBanner(banner());
    expect(presentation.title).toMatch(/no proposal here may be promoted/i);
    // The API's own fixed sentence is carried through verbatim, never rewritten.
    expect(presentation.note).toBe(banner().note);
    expect(presentation.counts).toEqual([
      ["Pages", 65],
      ["Link plans", 48],
      ["Review actions", 12],
    ]);
    expect(presentation.demonstrationMode).toBe(true);
    expect(presentation.minSampleCount).toBe(30);
    // Demonstration mode is described as a MECHANISM, not a promise.
    expect(presentation.enforcement).toMatch(/by construction/i);
    expect(presentation.enforcement).toMatch(/demonstration, not evidence/i);
  });

  it("says plainly when demonstration mode is off", () => {
    const presentation = describeCorpusBanner(banner({ demonstration_mode: false }));
    expect(presentation.demonstrationMode).toBe(false);
    expect(presentation.enforcement).toMatch(/can succeed/i);
  });

  it("knows when a measurement is below the sample floor", () => {
    expect(belowSampleFloor(29, banner())).toBe(true);
    expect(belowSampleFloor(30, banner())).toBe(false);
  });
});

describe("learning enums", () => {
  it("labels every proposal and skill status", () => {
    expect([...PROPOSAL_STATUSES]).toEqual(PROPOSAL);
    expect([...SKILL_STATUSES]).toEqual(SKILL);
    for (const status of PROPOSAL) expect(describeProposalStatus(status).known).toBe(true);
    for (const status of SKILL) expect(describeSkillStatus(status).known).toBe(true);
    // Only a human promotes, and the labels have to say so.
    expect(describeProposalStatus("promoted").description).toMatch(/by a human/i);
    expect(describeProposalStatus("promoted").description).toMatch(/never automatically/i);
    expect(describeSkillStatus("promoted").description).toMatch(/Promoted by a human/i);
    expect(describeSkillStatus("draft").description).toMatch(/not in use/i);
  });

  it("labels gate results and never treats inconclusive as a pass", () => {
    expect([...GATE_RESULTS]).toEqual(["pass", "fail", "inconclusive"]);
    expect(describeGateResult("pass").tone).toBe("positive");
    expect(describeGateResult("fail").tone).toBe("critical");
    expect(describeGateResult("inconclusive")).toMatchObject({ tone: "caution" });
    expect(describeGateResult("inconclusive").description).toMatch(/never treated as a pass/i);
    for (const code of GATE_CODES) expect(describeGateReasonCode(code)).not.toMatch(/^Unknown/);
  });

  it("labels every promotion refusal, including the demonstration-mode one", () => {
    for (const refusal of REFUSALS) expect(describePromotionRefusal(refusal)).not.toMatch(/^Unknown/);
    expect(describePromotionRefusal("demonstration_mode")).toMatch(/nothing may be promoted on this data/i);
    expect(describePromotionRefusal("not_a_human")).toMatch(/agent never can/i);
    // Behavioural data may only BLOCK a promotion, never justify one.
    expect(describePromotionRefusal("behavioural_block")).toMatch(/only block, never justify/i);
  });

  it("labels kinds, reasons, targets, sources and modes", () => {
    expect(describeProposalKind("policy")).toBe("Policy");
    expect(describeProposalKind("skill")).toBe("Skill");
    for (const reason of PROPOSAL_REJECTION_REASONS) {
      expect(describeProposalRejectionReason(reason)).not.toMatch(/^Unknown/);
    }
    for (const kind of ["ranking", "recommendation", "agents"]) {
      expect(describeTargetKind(kind)).not.toMatch(/^Unknown/);
    }
    expect(describeSkillSource("reflection")).toMatch(/reflection job/i);
    expect(describeEvaluationMode("canary")).toBe("Canary");
    expect(describeEvalSuite("anchor-selection")).toBe("Anchor selection");
    expect(describeEvalSuite("critic")).toBe("Critic");
    expect(describeEvalSuite("  ")).toBe("Unknown (empty)");
  });

  it("degrades unexpected values to an explicit Unknown label", () => {
    expect(describeProposalStatus("teleported")).toMatchObject({ label: "Unknown (teleported)", known: false });
    expect(describeSkillStatus(" ").label).toBe("Unknown (empty)");
    expect(describeGateResult("x").known).toBe(false);
    expect(describePromotionRefusal("toString")).toBe("Unknown (toString)");
    expect(describeTargetKind("constructor")).toBe("Unknown (constructor)");
  });
});

describe("promotion preview", () => {
  it("reports a blocked promotion with every refusal that applies", () => {
    const blocked = describePromotionBlock(["demonstration_mode", "sample_too_small"]);
    expect(blocked.label).toBe("Promotion refused (2 reasons)");
    expect(blocked.tone).toBe("critical");
    expect(blocked.description).toMatch(/nothing may be promoted on this data/i);
    expect(blocked.description).toMatch(/Too few samples/i);
    expect(describePromotionBlock(["demonstration_mode"]).label).toBe("Promotion refused");
    expect(describePromotionBlock([])).toMatchObject({ label: "No refusal recorded", tone: "neutral" });
  });
});

describe("advancing a proposal", () => {
  it("requires a structured reason for a rejecting transition", () => {
    expect(reasonRequiredFor("rejected")).toBe(true);
    expect(reasonRequiredFor("rolled_back")).toBe(true);
    expect(reasonRequiredFor("withdrawn")).toBe(true);
    expect(reasonRequiredFor("promoted")).toBe(false);
    const missing = validateAdvanceInput({ toStatus: "rejected", reason: "", note: "", evaluationRunId: "" });
    expect(missing.reason).toMatch(/structured reason is required/i);
    expect(hasAdvanceErrors(missing)).toBe(true);
    expect(
      validateAdvanceInput({ toStatus: "rejected", reason: "gate_failed", note: "", evaluationRunId: "" }),
    ).toEqual({});
  });

  it("rejects an unknown status, an over-long note and a malformed run id", () => {
    expect(
      validateAdvanceInput({ toStatus: "teleported", reason: "", note: "", evaluationRunId: "" }).to_status,
    ).toBeDefined();
    expect(
      validateAdvanceInput({
        toStatus: "promoted",
        reason: "",
        note: "x".repeat(MAX_NOTE_CHARS + 1),
        evaluationRunId: "",
      }).note,
    ).toMatch(/500 characters or fewer/);
    expect(
      validateAdvanceInput({ toStatus: "promoted", reason: "", note: "", evaluationRunId: "nope" })
        .evaluation_run_id,
    ).toMatch(/must be a UUID/);
    expect(
      validateAdvanceInput({
        toStatus: "promoted",
        reason: "",
        note: "",
        evaluationRunId: "66666666-6666-4666-8666-666666666666",
      }),
    ).toEqual({});
  });

  it("reports an outcome, and marks an idempotent replay as such", () => {
    const fresh = describeAdvanceOutcome("promoted", false);
    expect(fresh.status).toBe("done");
    expect(fresh.status === "done" && fresh.message).toMatch(/now Promoted/);
    const replay = describeAdvanceOutcome("promoted", true);
    expect(replay.status === "done" && replay.message).toMatch(/nothing changed twice/i);
  });

  it("never claims nothing changed when the outcome is unknown", () => {
    for (const error of [{ kind: "timeout" }, { kind: "api_error", status: 503 }] as const) {
      expect(advanceOutcomeUnknown(error)).toBe(true);
      const message = advanceErrorMessage({ ...error, message: "x" });
      expect(message).toMatch(/UNKNOWN/);
      expect(message).not.toMatch(/nothing was changed/i);
    }
    // A 409 is very often the demonstration-mode refusal: it is a decision, not a blip.
    const conflict = advanceErrorMessage({ kind: "api_error", status: 409, message: "x", detail: "Demonstration mode." });
    expect(advanceOutcomeUnknown({ kind: "api_error", status: 409 })).toBe(false);
    expect(conflict).toMatch(/not allowed from the proposal's current status, or a gate refused it/i);
    expect(conflict).toMatch(/Demonstration mode\./);
    expect(advanceErrorMessage({ kind: "api_error", status: 403, message: "x" })).toMatch(/promote permission/);
  });

  it("concludes an operation only on a recorded outcome", () => {
    expect(concludesAdvance({ status: "idle" })).toBe(false);
    expect(concludesAdvance({ status: "done", message: "", replayed: false, toStatus: "promoted" })).toBe(true);
    expect(concludesAdvance({ status: "error", message: "", outcomeUnknown: false, code: "conflict" })).toBe(true);
    // An unknown outcome keeps the id so a retry replays rather than re-applying.
    expect(concludesAdvance({ status: "error", message: "", outcomeUnknown: true, code: "conflict" })).toBe(false);
  });
});

describe("filters and routes", () => {
  it("accepts only contract values", () => {
    expect(parseProposalStatus({ status: "canary" })).toBe("canary");
    expect(parseProposalStatus({ status: "nope" })).toBeUndefined();
    expect(parseProposalKind({ kind: "skill" })).toBe("skill");
    expect(parseProposalKind({ kind: "toString" })).toBeUndefined();
    expect(parseSkillStatus({ status: "retired" })).toBe("retired");
    expect(parseSkillStatus({})).toBeUndefined();
    expect(parseGateResult({ gate_result: "inconclusive" })).toBe("inconclusive");
    expect(parseGateResult({ gate_result: "maybe" })).toBeUndefined();
  });

  it("escapes ids and names in hrefs", () => {
    expect(proposalHref("a/b")).toBe("/learning/proposals/a%2Fb");
    expect(skillHref("../etc")).toBe("/learning/skills/..%2Fetc");
  });
});
