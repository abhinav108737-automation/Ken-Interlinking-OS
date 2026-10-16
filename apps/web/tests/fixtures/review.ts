import type {
  BulkReviewResult,
  DashboardSummary,
  EventRecord,
  PoliciesView,
  PolicyDocument,
  PrincipalView,
  ReviewActionView,
  ReviewResult,
  Role,
  SessionView,
  TargetOption,
} from "@/lib/api-client";

import { OTHER_PAGE_ID } from "./inventory";
import { ANCHOR, OLD_PLAN_ID, PLAN_ID } from "./recommendations";

export const NEW_PLAN_ID = "77777777-7777-4777-8777-777777777777";
export const OPERATION_ID = "88888888-8888-4888-8888-888888888888";
export const THIRD_PAGE_ID = "99999999-9999-4999-8999-999999999999";
export const TOKEN = "tok_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v";

const PERMISSIONS: Readonly<Record<Role, PrincipalView["permissions"]>> = {
  viewer: ["read"],
  reviewer: ["read", "review"],
  operator_admin: ["read", "review", "read_audit", "read_settings", "deploy", "promote"],
};

export function principal(role: Role = "reviewer", overrides: Partial<PrincipalView> = {}): PrincipalView {
  return {
    username: "jane",
    display_name: "Jane Reviewer",
    role,
    permissions: [...PERMISSIONS[role]],
    session_expires_at: "2026-10-14T23:00:00Z",
    ...overrides,
  };
}

export function sessionView(overrides: Partial<SessionView> = {}): SessionView {
  return { token: TOKEN, expires_at: "2026-10-14T23:00:00Z", user: principal(), ...overrides };
}

export function reviewAction(overrides: Partial<ReviewActionView> = {}): ReviewActionView {
  return {
    id: 1,
    operation_id: OPERATION_ID,
    action: "reject",
    actor: "user:jane",
    plan_id: PLAN_ID,
    new_plan_id: null,
    new_plan_status: null,
    from_status: "awaiting_approval",
    to_status: "rejected",
    reasons: ["irrelevant_destination"],
    note: "",
    anchor_before: ANCHOR,
    anchor_after: null,
    target_before: OTHER_PAGE_ID,
    target_after: null,
    bulk_operation_id: null,
    created_at: "2026-10-14T10:00:00Z",
    ...overrides,
  };
}

export function reviewResult(overrides: Partial<ReviewResult> = {}): ReviewResult {
  return {
    action_id: 1,
    operation_id: OPERATION_ID,
    action: "approve",
    plan_id: PLAN_ID,
    status: "approved",
    new_plan_id: null,
    new_plan_status: null,
    idempotent_replay: false,
    ...overrides,
  };
}

export function bulkResult(overrides: Partial<BulkReviewResult> = {}): BulkReviewResult {
  return {
    operation_id: OPERATION_ID,
    action: "approve",
    items: [
      { plan_id: PLAN_ID, ok: true, status: "approved", error_code: null },
      { plan_id: OLD_PLAN_ID, ok: false, status: null, error_code: "stale_page_version" },
    ],
    idempotent_replay: false,
    ...overrides,
  };
}

export function targetOption(overrides: Partial<TargetOption> = {}): TargetOption {
  return {
    page_id: THIRD_PAGE_ID,
    canonical_url: "https://www.example.com/reports/solid-state-battery-market",
    title: "Solid-state battery market report",
    page_type: "report",
    market_id: "solid_state_batteries",
    relevance: 0.71,
    ...overrides,
  };
}

export function dashboardSummary(overrides: Partial<DashboardSummary> = {}): DashboardSummary {
  return {
    pages_total: 120,
    pages_live: 118,
    pages_analyzed: 100,
    pages_protected: 4,
    plans_by_status: { awaiting_approval: 7, approved: 3, rejected: 2 },
    awaiting_approval: 7,
    opportunities_by_kind: { orphan: 5 },
    agent_runs_by_outcome: { handed_off: 9 },
    reviews_by_action: { approve: 3, reject: 2 },
    recent_review_actions: [reviewAction()],
    latest_jobs: [],
    auto_approve_enabled: false,
    ...overrides,
  };
}

export function eventRecord(overrides: Partial<EventRecord> = {}): EventRecord {
  return {
    id: 10,
    event_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    event_name: "link.approved",
    event_version: 1,
    occurred_at: "2026-10-14T10:00:00Z",
    emitted_by: "user:jane",
    subject_type: "link_plan",
    subject_id: PLAN_ID,
    payload: { plan_id: PLAN_ID, actor: "user:jane" },
    payload_status: "ok",
    correlation_id: OPERATION_ID,
    ...overrides,
  };
}

export function policyDocument(overrides: Partial<PolicyDocument> = {}): PolicyDocument {
  return {
    kind: "ranking",
    version: 1,
    path: "policies/ranking/ranking_weights.v1.yaml",
    sha256: "b".repeat(64),
    content: { name: "ranking_weights", weights: { semantic: 0.5, taxonomy: 0.3 } },
    ...overrides,
  };
}

export function policiesView(overrides: Partial<PoliciesView> = {}): PoliciesView {
  return { policies: [policyDocument()], auto_approve_enabled: false, ...overrides };
}
