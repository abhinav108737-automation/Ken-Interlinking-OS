import { describe, expect, it } from "vitest";

import {
  AGENT_ERROR_CODES,
  AGENT_NODES,
  AGENT_OUTCOMES,
  AGENT_RUN_STATUSES,
  AGENT_TRIGGERS,
  CRITIC_REASONS,
  CRITIC_VERDICTS,
  FALLBACK_REASONS,
  MODEL_ROLES,
  STEP_STATUSES,
  TOOL_NAMES,
  TOOL_PERMISSIONS,
  TRIAGE_DECISIONS,
  agentRunDuration,
  agentRunHref,
  describeAgentErrorCode,
  describeAgentNode,
  describeAgentOutcome,
  describeAgentRunStatus,
  describeAgentTrigger,
  describeCriticReason,
  describeCriticVerdict,
  describeFallbackReason,
  describeModelRole,
  describeStepStatus,
  describeToolName,
  describeToolPermission,
  describeTriageDecision,
  formatAgentJson,
} from "@/lib/agents";
import type { components } from "@/lib/types.gen";

type Schemas = components["schemas"];

// Every value of each contract enum, spelled out and typed by the generated
// schema, so this test (as well as the Record types in lib/agents.ts) notices
// a contract change.
const RUN_STATUSES: Schemas["AgentRunStatus"][] = ["running", "awaiting_approval", "completed", "failed"];
const OUTCOMES: Schemas["AgentOutcome"][] = ["recommend", "no_action", "needs_human_review", "failed"];
const TRIGGERS: Schemas["AgentTrigger"][] = ["manual", "batch"];
const TRIAGE: Schemas["TriageDecision"][] = ["no_action", "reindex", "optimize", "repair_link", "review_performance"];
const NODES: Schemas["AgentNode"][] = [
  "load_page", "triage", "understand", "discover", "plan", "critique", "check_invariants", "policy_gate", "finalize",
];
const STEPS: Schemas["StepStatus"][] = ["succeeded", "skipped", "failed"];
const FALLBACKS: Schemas["FallbackReason"][] = ["model_disabled", "model_unavailable", "model_timeout", "schema_violation"];
const ERRORS: Schemas["AgentErrorCode"][] = [
  "page_not_found", "tool_failed", "timeout", "invariant_violated", "handoff_conflict", "orphaned", "internal",
];
const VERDICTS: Schemas["CriticVerdict"][] = ["accept", "reject", "uncertain"];
const REASONS: Schemas["CriticReason"][] = [
  "natural_anchor", "relevant_destination", "misleading_anchor", "weak_relevance", "reader_journey_mismatch",
  "over_linking", "low_confidence",
];
const TOOLS: Schemas["ToolName"][] = [
  "get_page", "get_analysis", "get_eligibility", "list_proposed_plans", "check_plan", "hand_off_plans",
];
const PERMISSIONS: Schemas["ToolPermission"][] = ["read", "review"];
const ROLES: Schemas["ModelRole"][] = ["fast", "reasoning", "embedding"];

/** Each value has a known, non-"Unknown" label, and labels are distinct. */
function expectLabelled(values: readonly string[], describe: (value: string) => string) {
  const labels = values.map(describe);
  for (const text of labels) expect(text).not.toMatch(/^Unknown/);
  expect(new Set(labels).size).toBe(values.length);
}

describe("agent enum labels", () => {
  it("cover exactly the contract's values, in contract order", () => {
    expect([...AGENT_RUN_STATUSES]).toEqual(RUN_STATUSES);
    expect([...AGENT_OUTCOMES]).toEqual(OUTCOMES);
    expect([...AGENT_TRIGGERS]).toEqual(TRIGGERS);
    expect([...TRIAGE_DECISIONS]).toEqual(TRIAGE);
    expect([...AGENT_NODES]).toEqual(NODES);
    expect([...STEP_STATUSES]).toEqual(STEPS);
    expect([...FALLBACK_REASONS]).toEqual(FALLBACKS);
    expect([...AGENT_ERROR_CODES]).toEqual(ERRORS);
    expect([...CRITIC_VERDICTS]).toEqual(VERDICTS);
    expect([...CRITIC_REASONS]).toEqual(REASONS);
    expect([...TOOL_NAMES]).toEqual(TOOLS);
    expect([...TOOL_PERMISSIONS]).toEqual(PERMISSIONS);
    expect([...MODEL_ROLES]).toEqual(ROLES);
  });

  it("label every value with distinct text", () => {
    for (const value of RUN_STATUSES) expect(describeAgentRunStatus(value).known).toBe(true);
    for (const value of OUTCOMES) expect(describeAgentOutcome(value).known).toBe(true);
    for (const value of STEPS) expect(describeStepStatus(value).known).toBe(true);
    for (const value of VERDICTS) expect(describeCriticVerdict(value).known).toBe(true);
    expectLabelled(RUN_STATUSES, (v) => describeAgentRunStatus(v).label);
    expectLabelled(OUTCOMES, (v) => describeAgentOutcome(v).label);
    expectLabelled(STEPS, (v) => describeStepStatus(v).label);
    expectLabelled(VERDICTS, (v) => describeCriticVerdict(v).label);
    expectLabelled(TRIGGERS, describeAgentTrigger);
    expectLabelled(TRIAGE, describeTriageDecision);
    expectLabelled(NODES, describeAgentNode);
    expectLabelled(FALLBACKS, describeFallbackReason);
    expectLabelled(ERRORS, describeAgentErrorCode);
    expectLabelled(REASONS, describeCriticReason);
    expectLabelled(TOOLS, describeToolName);
    expectLabelled(PERMISSIONS, describeToolPermission);
    expectLabelled(ROLES, describeModelRole);
  });

  it("never present a handed-off run as approved", () => {
    const handedOff = describeAgentRunStatus("awaiting_approval");
    expect(handedOff.label).toBe("Awaiting approval");
    expect(handedOff.description).toMatch(/nothing is approved or deployed/i);
    expect(describeAgentRunStatus("failed").tone).toBe("critical");
    expect(describeAgentOutcome("needs_human_review")).toMatchObject({ label: "Needs human review", tone: "caution" });
    expect(describeCriticVerdict("reject").description).toMatch(/only a person can reject/i);
  });

  it("show a pending outcome for a run that has none", () => {
    expect(describeAgentOutcome(null)).toMatchObject({ label: "Pending", known: true });
  });

  it("degrade unexpected values to an explicit Unknown label", () => {
    expect(describeAgentRunStatus("paused")).toMatchObject({ label: "Unknown (paused)", known: false, tone: "neutral" });
    expect(describeAgentOutcome("escalate")).toMatchObject({ label: "Unknown (escalate)", known: false });
    expect(describeStepStatus("retrying").label).toBe("Unknown (retrying)");
    expect(describeCriticVerdict("maybe").label).toBe("Unknown (maybe)");
    expect(describeAgentRunStatus(" ").label).toBe("Unknown (empty)");
    expect(describeAgentTrigger("cron")).toBe("Unknown (cron)");
    expect(describeTriageDecision("x")).toBe("Unknown (x)");
    expect(describeAgentNode("x")).toBe("Unknown (x)");
    expect(describeFallbackReason("x")).toBe("Unknown (x)");
    expect(describeAgentErrorCode("x")).toBe("Unknown (x)");
    expect(describeCriticReason("x")).toBe("Unknown (x)");
    expect(describeToolName("delete_everything")).toBe("Unknown (delete_everything)");
    expect(describeToolPermission("write")).toBe("Unknown (write)");
    expect(describeModelRole("x")).toBe("Unknown (x)");
    // Inherited object keys are not labels.
    expect(describeAgentNode("toString")).toBe("Unknown (toString)");
    expect(describeAgentRunStatus("constructor").known).toBe(false);
  });
});

describe("agent formatting", () => {
  it("formats a run's duration, or says it is in progress", () => {
    expect(
      agentRunDuration({ started_at: "2026-10-14T10:00:00Z", finished_at: "2026-10-14T10:00:03.500Z", status: "completed" }),
    ).toBe("3.5 s");
    expect(agentRunDuration({ started_at: "2026-10-14T10:00:00Z", finished_at: null, status: "running" })).toBe("In progress");
    expect(agentRunDuration({ started_at: "2026-10-14T10:00:00Z", finished_at: null, status: "failed" })).toBe("—");
  });

  it("renders step output as compact JSON text with credential-like keys withheld", () => {
    expect(formatAgentJson({})).toBe("{}");
    expect(formatAgentJson({ reviewed: 2, refs: ["c0", "c1"] })).toBe('{"reviewed":2,"refs":["c0","c1"]}');
    const text = formatAgentJson({ nested: { api_token: "s3cret" } });
    expect(text).not.toContain("s3cret");
    expect(text).toContain("[withheld]");
  });

  it("builds encoded run hrefs", () => {
    expect(agentRunHref("abc-123")).toBe("/agents/abc-123");
    expect(agentRunHref("a/b")).toBe("/agents/a%2Fb");
  });
});
