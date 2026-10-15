import type {
  ChangePreview,
  DeploymentDetail,
  DeploymentItemView,
  DeploymentView,
  DryRunResult,
  VerificationRun,
} from "@/lib/api-client";

import { OTHER_PAGE_ID, PAGE_ID } from "./inventory";
import { ANCHOR, BLOCK_TEXT, OLD_PLAN_ID, PLAN_ID } from "./recommendations";

export const DEPLOYMENT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
export const DEPLOY_OPERATION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

export const HTML_BEFORE = '<p>The global lithium-ion battery market is growing.<script>window.__pwned = true</script></p>';
export const HTML_AFTER =
  '<p>The global <a href="https://www.example.com/reports/lithium-ion-battery-market" data-ken-link-id="x">lithium-ion battery market</a> is growing.<img src=x onerror="alert(1)"></p>';

export function changePreview(overrides: Partial<ChangePreview> = {}): ChangePreview {
  return {
    plan_id: PLAN_ID,
    anchor_text: ANCHOR,
    href: "https://www.example.com/reports/lithium-ion-battery-market",
    block_text: BLOCK_TEXT,
    anchor_start: 11,
    anchor_end: 37,
    html_before: HTML_BEFORE,
    html_after: HTML_AFTER,
    ...overrides,
  };
}

export function dryRunResult(overrides: Partial<DryRunResult> = {}): DryRunResult {
  return {
    source_page_id: PAGE_ID,
    cms_page_id: "cms-42",
    cms_version: "v7",
    valid: true,
    changes: [changePreview()],
    issues: [],
    ...overrides,
  };
}

export function deploymentView(overrides: Partial<DeploymentView> = {}): DeploymentView {
  return {
    id: DEPLOYMENT_ID,
    operation_id: DEPLOY_OPERATION_ID,
    source_page_id: PAGE_ID,
    source_url: "https://www.example.com/reports/widgets",
    cms_provider: "simulated",
    cms_page_id: "cms-42",
    status: "verified",
    plan_ids: [PLAN_ID],
    previous_version: "v7",
    new_version: "v8",
    actor: "user:olivia",
    error_code: null,
    created_at: "2026-10-15T08:00:00Z",
    applied_at: "2026-10-15T08:00:02Z",
    verified_at: "2026-10-15T08:00:03Z",
    rolled_back_at: null,
    idempotent_replay: false,
    ...overrides,
  };
}

export function deploymentItem(overrides: Partial<DeploymentItemView> = {}): DeploymentItemView {
  return {
    plan_id: PLAN_ID,
    plan_status: "verified",
    anchor_text: ANCHOR,
    target_url: "https://www.example.com/reports/lithium-ion-battery-market",
    ...overrides,
  };
}

export function verificationRun(overrides: Partial<VerificationRun> = {}): VerificationRun {
  return {
    id: 1,
    run_at: "2026-10-15T08:00:03Z",
    passed: true,
    inconclusive: false,
    http_checked: true,
    checks: [
      { plan_id: PLAN_ID, check: "link_present_once", outcome: "passed", detail: null },
      { plan_id: PLAN_ID, check: "target_reachable", outcome: "inconclusive", detail: "http_timeout" },
    ],
    ...overrides,
  };
}

export function deploymentDetail(overrides: Partial<DeploymentDetail> = {}): DeploymentDetail {
  return {
    ...deploymentView(),
    items: [deploymentItem()],
    preview: [changePreview()],
    verifications: [verificationRun()],
    rollback_reason: null,
    rollback_note: null,
    ...overrides,
  };
}

/** A two-plan deployment (for per-plan rollback). */
export function twoPlanDeployment(overrides: Partial<DeploymentDetail> = {}): DeploymentDetail {
  return deploymentDetail({
    plan_ids: [PLAN_ID, OLD_PLAN_ID],
    items: [deploymentItem(), deploymentItem({ plan_id: OLD_PLAN_ID, anchor_text: "battery recycling", target_url: `https://www.example.com/p/${OTHER_PAGE_ID}` })],
    ...overrides,
  });
}
