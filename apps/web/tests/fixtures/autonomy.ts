import type {
  AutonomyDecisionView,
  AutonomyHealth,
  AutonomySummary,
  BlockedActionView,
  PublishAttemptView,
} from "@/lib/api-client";

import { OTHER_PAGE_ID, PAGE_ID } from "./inventory";
import { ANCHOR, PLAN_ID } from "./recommendations";

const SOURCE_URL = "https://www.example.com/reports/widgets";
const TARGET_URL = "https://www.example.com/reports/lithium-ion-battery-market";

/**
 * `GET /autonomy/mode`. The default is SHADOW with publishing disabled, exactly
 * as ADR-0018 §2 makes it, so a test that wants a publishing system has to say
 * so explicitly rather than getting one by accident.
 */
export function autonomyHealth(overrides: Partial<AutonomyHealth> = {}): AutonomyHealth {
  return {
    mode: "shadow",
    publishing_enabled: false,
    autonomous_acknowledged: false,
    allow_production: false,
    cms_provider: "filesystem",
    cms_writes_enabled: false,
    calibration_version: null,
    policy_version: 1,
    verification_profile_default: "standard",
    ...overrides,
  };
}

export function autonomySummary(overrides: Partial<AutonomySummary> = {}): AutonomySummary {
  return {
    mode: "shadow",
    auto_published: 4,
    blocked_plans: 3,
    rolled_back: 1,
    blocks_by_code: { protected_page: 2, stale_vector: 1 },
    decisions_by_decision: { auto_publish: 4, block: 3 },
    decisions_by_mode: { shadow: 7 },
    ...overrides,
  };
}

export function autonomyDecision(overrides: Partial<AutonomyDecisionView> = {}): AutonomyDecisionView {
  return {
    id: 1,
    plan_id: PLAN_ID,
    plan_status: "autonomy_decided",
    decision: "block",
    mode: "shadow",
    would_have_decision: "auto_publish",
    risk_tier: "medium",
    critic_outcome: "accept",
    critic_verdict: "accept",
    confidence: 0.63,
    calibration_version: null,
    policy_version: 1,
    engine_version: "autonomy-1.0.0",
    rollback_available: true,
    verification_profile: "standard",
    blocks: ["protected_page"],
    source_url: SOURCE_URL,
    target_url: TARGET_URL,
    evaluated_at: "2026-10-16T08:00:00Z",
    ...overrides,
  };
}

export function blockedAction(overrides: Partial<BlockedActionView> = {}): BlockedActionView {
  return {
    plan_id: PLAN_ID,
    source_page_id: PAGE_ID,
    target_page_id: OTHER_PAGE_ID,
    source_url: SOURCE_URL,
    target_url: TARGET_URL,
    anchor_text: ANCHOR,
    blocks: ["protected_page"],
    mode: "shadow",
    risk_tier: "medium",
    overridden: false,
    // Why the plan is blocked, in words, as the engine recorded it: the fired
    // codes, or -- for a deferral, whose block list is empty -- "deferred".
    reason: "Protected page.",
    decided_at: "2026-10-16T08:00:00Z",
    ...overrides,
  };
}

export function publishAttempt(overrides: Partial<PublishAttemptView> = {}): PublishAttemptView {
  return {
    id: 1,
    plan_id: PLAN_ID,
    source_page_id: PAGE_ID,
    target_page_id: OTHER_PAGE_ID,
    attempt: 1,
    decision: "auto_publish",
    mode: "autonomous",
    verification_profile: "standard",
    succeeded: true,
    rolled_back: false,
    failure_code: null,
    cooldown_until: null,
    occurred_at: "2026-10-16T09:00:00Z",
    ...overrides,
  };
}
