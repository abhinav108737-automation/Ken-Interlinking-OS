/**
 * Pure presentation logic for health and readiness values.
 *
 * Every mapping is a `Record` keyed by a generated contract enum, so adding a
 * value to the frozen OpenAPI contract and regenerating types makes `tsc` fail
 * here until the new value is given a label. Values that arrive at runtime but
 * are not in the contract (a newer backend) degrade to an explicit "Unknown"
 * label instead of crashing or rendering nothing.
 *
 * No React, no fetch, no side effects: this module is unit-tested directly.
 */
import type { components } from "./types.gen";

export type HealthStatus = components["schemas"]["HealthStatus"];
export type ReadinessStatus = components["schemas"]["ReadinessStatus"];
export type CheckKind = components["schemas"]["CheckKind"];
export type DependencyCheck = components["schemas"]["DependencyCheck"];

/** Visual tone. Always paired with a text label: colour is never the only signal. */
export type Tone = "positive" | "caution" | "critical" | "neutral";

export interface StatusPresentation {
  /** Short text shown to the user. Carries the meaning on its own. */
  readonly label: string;
  /** One-sentence explanation, suitable for a description or tooltip. */
  readonly description: string;
  readonly tone: Tone;
  /** False when the value is not part of the contract this build was generated from. */
  readonly known: boolean;
}

const HEALTH_STATUS_PRESENTATION: Readonly<Record<HealthStatus, StatusPresentation>> = {
  ok: {
    label: "OK",
    description: "The dependency responded as expected.",
    tone: "positive",
    known: true,
  },
  degraded: {
    label: "Degraded",
    description: "The dependency responded but is not fully functional.",
    tone: "caution",
    known: true,
  },
  unavailable: {
    label: "Unavailable",
    description: "The dependency could not be reached or is not installed.",
    tone: "critical",
    known: true,
  },
  error: {
    label: "Error",
    description: "The probe for this dependency failed unexpectedly.",
    tone: "critical",
    known: true,
  },
  skipped: {
    label: "Skipped",
    description: "The probe was not run in this configuration.",
    tone: "neutral",
    known: true,
  },
};

const READINESS_STATUS_PRESENTATION: Readonly<Record<ReadinessStatus, StatusPresentation>> = {
  ready: {
    label: "Ready",
    description: "All dependency checks passed.",
    tone: "positive",
    known: true,
  },
  degraded: {
    label: "Degraded",
    description: "All required dependencies are OK; at least one optional dependency is not.",
    tone: "caution",
    known: true,
  },
  not_ready: {
    label: "Not ready",
    description: "At least one required dependency failed.",
    tone: "critical",
    known: true,
  },
};

const CHECK_KIND_LABEL: Readonly<Record<CheckKind, string>> = {
  database: "Database",
  schema: "Schema",
  extension: "Extension",
  model_server: "Model server",
  cms: "CMS",
  vector_store: "Vector store",
};

export const HEALTH_STATUSES = Object.keys(HEALTH_STATUS_PRESENTATION) as readonly HealthStatus[];
export const READINESS_STATUSES = Object.keys(
  READINESS_STATUS_PRESENTATION,
) as readonly ReadinessStatus[];

function hasOwn<T extends object>(record: T, key: string): key is Extract<keyof T, string> {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function unknownPresentation(value: string, subject: string): StatusPresentation {
  const shown = value.trim() === "" ? "empty" : value;
  return {
    label: `Unknown (${shown})`,
    description: `The API reported a ${subject} value this console does not recognise.`,
    tone: "neutral",
    known: false,
  };
}

export function isHealthStatus(value: unknown): value is HealthStatus {
  return typeof value === "string" && hasOwn(HEALTH_STATUS_PRESENTATION, value);
}

export function isReadinessStatus(value: unknown): value is ReadinessStatus {
  return typeof value === "string" && hasOwn(READINESS_STATUS_PRESENTATION, value);
}

/** Maps a per-dependency status to its label and tone. Accepts any string. */
export function describeHealthStatus(status: string): StatusPresentation {
  return isHealthStatus(status)
    ? HEALTH_STATUS_PRESENTATION[status]
    : unknownPresentation(status, "dependency status");
}

/** Maps the aggregate readiness verdict to its label and tone. Accepts any string. */
export function describeReadinessStatus(status: string): StatusPresentation {
  return isReadinessStatus(status)
    ? READINESS_STATUS_PRESENTATION[status]
    : unknownPresentation(status, "readiness status");
}

export function describeCheckKind(kind: string): string {
  return hasOwn(CHECK_KIND_LABEL, kind) ? CHECK_KIND_LABEL[kind] : `Unknown (${kind})`;
}

export function requirementLabel(required: boolean): "Required" | "Optional" {
  return required ? "Required" : "Optional";
}

/** Formats a non-negative millisecond duration for display. */
export function formatLatency(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) {
    return "n/a";
  }
  if (ms < 1) {
    return "<1 ms";
  }
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  return `${(ms / 1000).toFixed(2)} s`;
}

/**
 * Keys that look credential-like are withheld even though the contract says
 * `detail` never carries secrets. Defence in depth: the console must not be the
 * place a backend regression becomes a leak.
 */
const SENSITIVE_KEY = /pass(word)?|secret|token|credential|api[_-]?key|authorization|dsn/i;

/** True when a key looks credential-like and its value must be withheld from display. */
export function isSensitiveKey(key: string): boolean {
  return SENSITIVE_KEY.test(key);
}

const MAX_DETAIL_ENTRIES = 4;
const MAX_VALUE_LENGTH = 60;

function formatDetailValue(value: unknown): string {
  let text: string;
  if (value === null) {
    text = "null";
  } else if (typeof value === "string") {
    text = value;
  } else if (typeof value === "number" || typeof value === "boolean") {
    text = String(value);
  } else if (Array.isArray(value)) {
    text = value.every((item) => ["string", "number", "boolean"].includes(typeof item))
      ? value.join(", ")
      : `[${value.length} items]`;
  } else {
    text = "{…}";
  }
  return text.length > MAX_VALUE_LENGTH ? `${text.slice(0, MAX_VALUE_LENGTH - 1)}…` : text;
}

/** Compact, secret-safe `key: value` summary of a check's `detail` object. */
export function summarizeDetail(detail: Readonly<Record<string, unknown>> | undefined): string {
  if (detail === undefined) {
    return "";
  }
  const entries = Object.entries(detail);
  const shown = entries.slice(0, MAX_DETAIL_ENTRIES).map(([key, value]) => {
    const rendered = SENSITIVE_KEY.test(key) ? "[withheld]" : formatDetailValue(value);
    return `${key}: ${rendered}`;
  });
  const hidden = entries.length - shown.length;
  if (hidden > 0) {
    shown.push(`+${hidden} more`);
  }
  return shown.join("; ");
}

export interface CheckSummary {
  /** The failure reason, when the API supplied one. */
  readonly error: string | null;
  /** Summary of probe facts, or an empty string. */
  readonly detail: string;
}

export function summarizeCheck(check: Pick<DependencyCheck, "error" | "detail">): CheckSummary {
  const error = typeof check.error === "string" && check.error.trim() !== "" ? check.error : null;
  return { error, detail: summarizeDetail(check.detail) };
}

/**
 * AUTO_APPROVE must be disabled globally in v1 (TRD.md section 20). If the API
 * ever reports it enabled, the console flags it as critical rather than neutral.
 */
export function describeAutoApprove(enabled: boolean): StatusPresentation {
  return enabled
    ? {
        label: "Enabled",
        description: "AUTO_APPROVE is enabled. It must be disabled in v1 (TRD section 20).",
        tone: "critical",
        known: true,
      }
    : {
        label: "Disabled",
        description: "Every production change requires human approval.",
        tone: "positive",
        known: true,
      };
}

export type ApiErrorKind =
  | "config"
  | "invalid_argument"
  | "network"
  | "timeout"
  | "api_error"
  | "http_status"
  | "invalid_body";

const API_ERROR_TITLE: Readonly<Record<ApiErrorKind, string>> = {
  config: "API address is misconfigured",
  invalid_argument: "Invalid request",
  network: "API unreachable",
  timeout: "API timed out",
  api_error: "API rejected the request",
  http_status: "Unexpected API response",
  invalid_body: "API response did not match the contract",
};

export function describeApiErrorKind(kind: ApiErrorKind): string {
  return API_ERROR_TITLE[kind];
}
