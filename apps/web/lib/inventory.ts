/**
 * Presentation logic for the Phase 1 content inventory screens.
 *
 * Portable and pure (see lib/status.ts for the same conventions): every enum
 * mapping is a `Record` keyed by a generated contract enum, so a new contract
 * value fails type-checking until it is labelled; an unexpected runtime value
 * degrades to an explicit "Unknown (…)" label instead of crashing.
 *
 * Types come from `lib/api-client.ts`, never from the env-reading `lib/api.ts`.
 */
import type {
  CanonicalStatus,
  IssueSeverity,
  LinkResolution,
  LinkType,
  PageInfo,
  PublicationState,
  RunStatus,
  SourceType,
} from "./api-client";
import type { StatusPresentation } from "./status";

// ---------------------------------------------------------------------------
// Enum presentations
// ---------------------------------------------------------------------------

function hasOwn<T extends object>(record: T, key: string): key is Extract<keyof T, string> {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function present<K extends string>(
  table: Readonly<Record<K, StatusPresentation>>,
  value: string,
  subject: string,
): StatusPresentation {
  if (hasOwn(table, value)) {
    return table[value];
  }
  const shown = value.trim() === "" ? "empty" : value;
  return {
    label: `Unknown (${shown})`,
    description: `The API reported a ${subject} value this console does not recognise.`,
    tone: "neutral",
    known: false,
  };
}

function label<K extends string>(table: Readonly<Record<K, string>>, value: string): string {
  return hasOwn(table, value) ? table[value] : `Unknown (${value === "" ? "empty" : value})`;
}

const PUBLICATION_STATE: Readonly<Record<PublicationState, StatusPresentation>> = {
  published: {
    label: "Published",
    description: "The source reports this page as published.",
    tone: "positive",
    known: true,
  },
  draft: {
    label: "Draft",
    description: "The source reports this page as a draft.",
    tone: "caution",
    known: true,
  },
  unpublished: {
    label: "Unpublished",
    description: "The source reports this page as unpublished.",
    tone: "neutral",
    known: true,
  },
  unknown: {
    label: "Unknown",
    description: "The source did not report a publication state.",
    tone: "neutral",
    known: true,
  },
};

const RUN_STATUS: Readonly<Record<RunStatus, StatusPresentation>> = {
  running: {
    label: "Running",
    description: "The run has not finished (or was killed before recording completion).",
    tone: "caution",
    known: true,
  },
  succeeded: {
    label: "Succeeded",
    description: "The run completed with no per-URL errors.",
    tone: "positive",
    known: true,
  },
  completed_with_errors: {
    label: "Completed with errors",
    description: "The run completed; some URLs recorded errors as issues.",
    tone: "caution",
    known: true,
  },
  failed: {
    label: "Failed",
    description: "The run itself could not complete.",
    tone: "critical",
    known: true,
  },
};

const LINK_RESOLUTION: Readonly<Record<LinkResolution, StatusPresentation>> = {
  resolved: {
    label: "Resolved",
    description: "The destination maps to a known, live page.",
    tone: "positive",
    known: true,
  },
  broken: {
    label: "Broken",
    description: "The destination returned 404/410, or maps to a page that is now gone.",
    tone: "critical",
    known: true,
  },
  unresolved: {
    label: "Unresolved",
    description: "The destination has not been observed (crawl limits, robots or an error).",
    tone: "caution",
    known: true,
  },
  not_applicable: {
    label: "Not applicable",
    description: "The link is not internal, so it is not resolved.",
    tone: "neutral",
    known: true,
  },
};

const ISSUE_SEVERITY: Readonly<Record<IssueSeverity, StatusPresentation>> = {
  info: { label: "Info", description: "Informational.", tone: "neutral", known: true },
  warning: { label: "Warning", description: "Worth a look.", tone: "caution", known: true },
  error: { label: "Error", description: "The URL could not be ingested.", tone: "critical", known: true },
};

const LINK_TYPE_LABEL: Readonly<Record<LinkType, string>> = {
  internal: "Internal",
  external: "External",
  fragment: "Fragment",
  non_http: "Non-HTTP",
  invalid: "Invalid",
};

const SOURCE_TYPE_LABEL: Readonly<Record<SourceType, string>> = {
  public_web: "Public web crawl",
  file_export: "File export",
  cms: "CMS",
};

const CANONICAL_STATUS_LABEL: Readonly<Record<CanonicalStatus, string>> = {
  self: "Self (declared canonical is this URL)",
  declared: "Declared (a different in-scope canonical is used)",
  missing: "Missing (none declared; final URL used)",
  out_of_scope: "Out of scope (declared canonical ignored)",
  invalid: "Invalid (declared canonical ignored)",
};

export const PUBLICATION_STATES = Object.keys(PUBLICATION_STATE) as readonly PublicationState[];
export const RUN_STATUSES = Object.keys(RUN_STATUS) as readonly RunStatus[];
export const ISSUE_SEVERITIES = Object.keys(ISSUE_SEVERITY) as readonly IssueSeverity[];
export const LINK_TYPES = Object.keys(LINK_TYPE_LABEL) as readonly LinkType[];
export const SOURCE_TYPES = Object.keys(SOURCE_TYPE_LABEL) as readonly SourceType[];

export const describePublicationState = (value: string) =>
  present(PUBLICATION_STATE, value, "publication state");
export const describeRunStatus = (value: string) => present(RUN_STATUS, value, "run status");
export const describeLinkResolution = (value: string) =>
  present(LINK_RESOLUTION, value, "link resolution");
export const describeIssueSeverity = (value: string) =>
  present(ISSUE_SEVERITY, value, "issue severity");
export const describeLinkType = (value: string) => label(LINK_TYPE_LABEL, value);
export const describeSourceType = (value: string) => label(SOURCE_TYPE_LABEL, value);
export const describeCanonicalStatus = (value: string) => label(CANONICAL_STATUS_LABEL, value);

/** The "gone" marker: the page's identity URL last answered 404/410. */
export const GONE_PRESENTATION: StatusPresentation = {
  label: "Gone",
  description: "The page's identity URL last answered 404 or 410. Nothing was deleted.",
  tone: "critical",
  known: true,
};

/** A page's display title: its title, or its canonical URL when it has none. */
export function pageDisplayTitle(page: { title: string | null; canonical_url: string }): string {
  const title = page.title?.trim() ?? "";
  return title === "" ? page.canonical_url : title;
}

// ---------------------------------------------------------------------------
// URL safety
// ---------------------------------------------------------------------------

/**
 * Returns an absolute http(s) URL safe to use as a link target, or `null`.
 *
 * Every page URL and href shown here came from crawled third-party content, so
 * it is untrusted. Only `http:` and `https:` survive: `javascript:`, `data:`,
 * `vbscript:`, `file:`, relative and unparseable values return `null` and must
 * be rendered as plain text.
 */
export function safeExternalHref(raw: string | null | undefined): string | null {
  if (raw === null || raw === undefined) {
    return null;
  }
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
}

// ---------------------------------------------------------------------------
// Query-string handling for list screens (GET forms; nothing here mutates).
// ---------------------------------------------------------------------------

/** Shape of Next.js `searchParams` after awaiting. */
export type SearchParams = Readonly<Record<string, string | string[] | undefined>>;

export const DEFAULT_PAGE_SIZE = 50;
export const MAX_PAGE_SIZE = 200;
export const MAX_OFFSET = 10_000_000;
export const MAX_QUERY_LENGTH = 200;

/** The first value of a query parameter, trimmed; `undefined` when absent or blank. */
export function firstParam(params: SearchParams, name: string): string | undefined {
  const raw = params[name];
  const value = (Array.isArray(raw) ? raw[0] : raw)?.trim();
  return value === undefined || value === "" ? undefined : value;
}

function parseInteger(raw: string | undefined): number | undefined {
  if (raw === undefined || !/^\d{1,9}$/.test(raw)) {
    return undefined;
  }
  return Number.parseInt(raw, 10);
}

/** `offset` within the contract's bounds; invalid → 0. */
export function parseOffset(params: SearchParams, name = "offset"): number {
  return Math.min(parseInteger(firstParam(params, name)) ?? 0, MAX_OFFSET);
}

/** `limit` within the contract's bounds (1–200); invalid → the default. */
export function parseLimit(params: SearchParams, name = "limit"): number {
  const value = parseInteger(firstParam(params, name));
  if (value === undefined || value < 1) {
    return DEFAULT_PAGE_SIZE;
  }
  return Math.min(value, MAX_PAGE_SIZE);
}

/** A positive integer parameter (e.g. `version`), or `undefined`. */
export function parsePositiveInt(params: SearchParams, name: string): number | undefined {
  const value = parseInteger(firstParam(params, name));
  return value !== undefined && value >= 1 ? value : undefined;
}

/** An enum parameter, accepted only if it is one of `allowed`; anything else is ignored. */
export function parseEnumParam<T extends string>(
  params: SearchParams,
  name: string,
  allowed: readonly T[],
): T | undefined {
  const value = firstParam(params, name);
  return value !== undefined && (allowed as readonly string[]).includes(value)
    ? (value as T)
    : undefined;
}

/** Free-text search, capped at the contract's 200 characters. */
export function parseSearchQuery(params: SearchParams, name = "q"): string | undefined {
  const value = firstParam(params, name);
  return value === undefined ? undefined : value.slice(0, MAX_QUERY_LENGTH);
}

/**
 * An internal href: `pathname` plus the non-empty params, URL-encoded. Used for
 * pagination and filter links; values are never interpolated by hand.
 */
export function buildHref(
  pathname: string,
  params: Readonly<Record<string, string | number | null | undefined>>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    search.set(key, String(value));
  }
  const query = search.toString();
  return query === "" ? pathname : `${pathname}?${query}`;
}

export interface PageWindow {
  /** 1-based index of the first item shown, or 0 when nothing is shown. */
  readonly first: number;
  /** 1-based index of the last item shown. */
  readonly last: number;
  readonly total: number;
  /** Offset of the previous page, or `null` on the first page. */
  readonly prevOffset: number | null;
  /** Offset of the next page, or `null` when there is no more. */
  readonly nextOffset: number | null;
}

/** Pagination arithmetic for an offset/limit `PageInfo` and the number of items received. */
export function pageWindow(page: PageInfo, itemCount: number): PageWindow {
  const limit = Math.max(1, page.limit);
  return {
    first: itemCount === 0 ? 0 : page.offset + 1,
    last: page.offset + itemCount,
    total: page.total,
    prevOffset: page.offset > 0 ? Math.max(0, page.offset - limit) : null,
    nextOffset: page.has_more ? page.offset + limit : null,
  };
}

/** Issue counts as rows, largest first, then by code. */
export function sortIssueCounts(
  counts: Readonly<Record<string, number>>,
): { code: string; count: number }[] {
  return Object.entries(counts)
    .map(([code, count]) => ({ code, count }))
    .sort((a, b) => b.count - a.count || a.code.localeCompare(b.code, "en"));
}
