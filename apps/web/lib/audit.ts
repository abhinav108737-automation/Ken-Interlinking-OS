/**
 * Presentation for the audit log and read-only settings (ADR-0013 sections 5 and 7).
 *
 * Portable and pure. Payloads and policy documents are rendered as TEXT
 * (compact JSON), never as HTML, with credential-looking keys withheld at any
 * depth (lib/format.ts) — defence in depth: neither should carry a secret.
 */
import type { EventName, PolicyKind } from "./api-client";
import { formatConfigValue } from "./format";
import { type SearchParams, firstParam } from "./inventory";

function hasOwn<T extends object>(record: T, key: string): key is Extract<keyof T, string> {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function unknownLabel(value: string): string {
  return `Unknown (${value.trim() === "" ? "empty" : value})`;
}

const EVENT_NAME_LABEL: Readonly<Record<EventName, string>> = {
  "page.created": "Page created",
  "page.updated": "Page updated",
  "page.deleted": "Page deleted",
  "crawl.completed": "Crawl completed",
  "link.proposed": "Link proposed",
  "link.approved": "Link approved",
  "link.rejected": "Link rejected",
  "link.deployed": "Link deployed",
  "link.verified": "Link verified",
  "link.failed": "Link failed",
  "link.rolled_back": "Link rolled back",
  "analytics.updated": "Analytics updated",
  "skill.proposed": "Skill proposed",
  "skill.promoted": "Skill promoted",
  "policy.proposed": "Policy proposed",
  "policy.promoted": "Policy promoted",
  "worker.heartbeat": "Worker heartbeat",
  "job.completed": "Job completed",
};

const POLICY_KIND: Readonly<Record<PolicyKind, { readonly label: string; readonly description: string }>> = {
  taxonomy: { label: "Taxonomy", description: "Markets, geographies and page types used by content intelligence." },
  recommendation: { label: "Recommendation", description: "Eligibility, anchor and placement rules of the engine." },
  ranking: { label: "Ranking", description: "Score component weights and thresholds." },
  agents: { label: "Agents", description: "Agent orchestration limits and review hand-off rules." },
  analytics: { label: "Analytics", description: "Measurement windows, source lag, bot filtering and collector limits." },
  learning: { label: "Learning", description: "Evaluation gates, the sample floor, the required stage path and the change allowlist." },
  autonomy: {
    label: "Autonomy",
    description: "Confidence floors, the retry budget and the risk thresholds that decide unattended publishing.",
  },
};

export const EVENT_NAMES = Object.keys(EVENT_NAME_LABEL) as readonly EventName[];
export const POLICY_KINDS = Object.keys(POLICY_KIND) as readonly PolicyKind[];

export const describeEventName = (value: string): string =>
  hasOwn(EVENT_NAME_LABEL, value) ? EVENT_NAME_LABEL[value] : unknownLabel(value);
export const describePolicyKind = (value: string): string =>
  hasOwn(POLICY_KIND, value) ? POLICY_KIND[value].label : unknownLabel(value);
export const describePolicyKindDetail = (value: string): string | null =>
  hasOwn(POLICY_KIND, value) ? POLICY_KIND[value].description : null;

/** An event payload as one line of compact JSON text, sensitive keys withheld. */
export function formatPayload(payload: Readonly<Record<string, unknown>>): string {
  return formatConfigValue(payload);
}

/** `event_name` filter: a contract event name, or `undefined` (any). */
export function parseEventNameParam(params: SearchParams, name = "event_name"): EventName | undefined {
  const value = firstParam(params, name);
  return value !== undefined && hasOwn(EVENT_NAME_LABEL, value) ? value : undefined;
}

export const AUDIT_REVIEWS_PATH = "/audit/reviews";
export const AUDIT_EVENTS_PATH = "/audit/events";
export const SETTINGS_POLICIES_PATH = "/settings/policies";
