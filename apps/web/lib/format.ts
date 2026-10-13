/**
 * Pure, deterministic display formatting.
 *
 * Portable (no Node, no `process`, no DOM): usable from server components, a
 * browser or a future desktop renderer. Output never depends on the host's
 * locale or time zone, so server and client render identical text.
 */
import { isSensitiveKey } from "./status";

/** Shown for an absent value. Screen readers announce it as "em dash"; pair it with context. */
export const NONE = "—";

const COUNT_FORMAT = new Intl.NumberFormat("en-US");

/** `1234` → `"1,234"`. Non-finite values render as `NONE`. */
export function formatCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? COUNT_FORMAT.format(value) : NONE;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * ISO-8601 timestamp → `"2026-10-13 12:00:05 UTC"`. Null/blank → `NONE`.
 * An unparseable value is shown verbatim rather than hidden.
 */
export function formatDateTime(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso.trim() === "") {
    return NONE;
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return (
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ` +
    `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())} UTC`
  );
}

/** Milliseconds → `"850 ms"`, `"12.3 s"`, `"3 min 05 s"`, `"2 h 04 min"`. Negative/non-finite → `NONE`. */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) {
    return NONE;
  }
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  const totalSeconds = ms / 1000;
  if (totalSeconds < 60) {
    return `${totalSeconds.toFixed(1)} s`;
  }
  const wholeSeconds = Math.round(totalSeconds);
  const hours = Math.floor(wholeSeconds / 3600);
  const minutes = Math.floor((wholeSeconds % 3600) / 60);
  const seconds = wholeSeconds % 60;
  return hours > 0 ? `${hours} h ${pad(minutes)} min` : `${minutes} min ${pad(seconds)} s`;
}

/**
 * Duration between two ISO timestamps. `null` end (still running, or never
 * completed) → `null`, so the caller can say why there is no duration.
 */
export function durationBetween(startIso: string, endIso: string | null): number | null {
  if (endIso === null) {
    return null;
  }
  const start = Date.parse(startIso);
  const end = Date.parse(endIso);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) {
    return null;
  }
  return end - start;
}

export const SHORT_HASH_LENGTH = 12;

/**
 * First 12 hex characters of a content hash, dropping an algorithm prefix such
 * as `sha256:`. Show the full value in a `title` alongside.
 */
export function shortHash(hash: string, length: number = SHORT_HASH_LENGTH): string {
  const bare = hash.includes(":") ? hash.slice(hash.lastIndexOf(":") + 1) : hash;
  return bare.length > length ? bare.slice(0, length) : bare;
}

/** Placeholder for a value withheld because its key looks credential-like. */
export const WITHHELD = "[withheld]";

/**
 * A configuration value as plain text. Strings verbatim; everything else as
 * JSON, with credential-looking keys withheld at any depth (defence in depth:
 * the contract says the snapshot is secret-free). Never throws.
 */
export function formatConfigValue(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (value === undefined) {
    return "undefined";
  }
  try {
    const text = JSON.stringify(value, (key, inner: unknown) =>
      key !== "" && isSensitiveKey(key) ? WITHHELD : inner,
    );
    return text ?? String(value);
  } catch {
    return "[unrepresentable value]";
  }
}

export interface DisplayEntry {
  readonly key: string;
  readonly value: string;
  readonly withheld: boolean;
}

/** Sorted key/value rows for a JSON object, with sensitive top-level keys withheld. */
export function toDisplayEntries(record: Readonly<Record<string, unknown>>): DisplayEntry[] {
  return Object.keys(record)
    .sort((a, b) => a.localeCompare(b, "en"))
    .map((key) => {
      const withheld = isSensitiveKey(key);
      return { key, withheld, value: withheld ? WITHHELD : formatConfigValue(record[key]) };
    });
}
