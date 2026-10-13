import type {
  ExistingLinkView,
  IngestionIssueView,
  IngestionRunDetail,
  IngestionRunSummary,
  PageDetail,
  PageInfo,
  PageSummary,
  PageVersionDetail,
  PageVersionSummary,
  RunCounters,
} from "@/lib/api-client";

export const PAGE_ID = "11111111-1111-4111-8111-111111111111";
export const OTHER_PAGE_ID = "22222222-2222-4222-8222-222222222222";
export const RUN_ID = "33333333-3333-4333-8333-333333333333";

export function pageInfo(overrides: Partial<PageInfo> = {}): PageInfo {
  return { total: 1, limit: 50, offset: 0, has_more: false, ...overrides };
}

export function pageSummary(overrides: Partial<PageSummary> = {}): PageSummary {
  return {
    id: PAGE_ID,
    canonical_url: "https://www.kenresearch.com/reports/widgets",
    content_hash: "sha256:0123456789abcdef0123456789abcdef",
    current_version: 2,
    external_id: null,
    external_link_count: 3,
    first_seen_at: "2026-10-01T08:00:00Z",
    gone_at: null,
    internal_link_count: 12,
    language: "en",
    last_changed_at: "2026-10-12T09:30:00Z",
    last_seen_at: "2026-10-13T10:00:00Z",
    page_type: null,
    publication_state: "published",
    title: "Widgets market report",
    word_count: 1534,
    ...overrides,
  };
}

export function versionDetail(overrides: Partial<PageVersionDetail> = {}): PageVersionDetail {
  return {
    page_id: PAGE_ID,
    version: 2,
    body_html: '<p>Hello <b>bold</b><script>window.__pwned = true</script><img src=x onerror="alert(1)"></p>',
    body_text: "Hello bold\n\nSecond paragraph of the widgets report.",
    canonical_status: "self",
    canonical_url: "https://www.kenresearch.com/reports/widgets",
    content_hash: "sha256:0123456789abcdef0123456789abcdef",
    created_at: "2026-10-12T09:30:00Z",
    declared_canonical_url: "https://www.kenresearch.com/reports/widgets",
    external_link_count: 3,
    fetched_at: "2026-10-12T09:29:58Z",
    hash_version: 1,
    headings: [
      { block_id: "b1", level: 1, text: "Widgets market" },
      { block_id: "b2", level: 2, text: "Market size" },
    ],
    http_status: 200,
    internal_link_count: 12,
    language: "en",
    meta_description: "All about widgets.",
    metadata: { "og:title": "Widgets", "csrf-token": "abc123" },
    modified_at: null,
    page_type: "report",
    publication_state: "published",
    published_at: "2026-09-01T00:00:00Z",
    redirect_chain: ["http://kenresearch.com/reports/widgets"],
    requested_url: "http://kenresearch.com/reports/widgets",
    robots_directives: ["index", "follow"],
    run_id: RUN_ID,
    source_id: "https://www.kenresearch.com/reports/widgets",
    source_type: "public_web",
    title: "Widgets market report",
    url: "https://www.kenresearch.com/reports/widgets",
    word_count: 1534,
    ...overrides,
  };
}

export function pageDetail(overrides: Partial<PageDetail> = {}): PageDetail {
  return { ...pageSummary(), current: versionDetail(), ...overrides };
}

export function versionSummary(overrides: Partial<PageVersionSummary> = {}): PageVersionSummary {
  return {
    page_id: PAGE_ID,
    version: 2,
    content_hash: "sha256:0123456789abcdef0123456789abcdef",
    created_at: "2026-10-12T09:30:00Z",
    external_link_count: 3,
    fetched_at: "2026-10-12T09:29:58Z",
    hash_version: 1,
    http_status: 200,
    internal_link_count: 12,
    publication_state: "published",
    run_id: RUN_ID,
    source_id: "https://www.kenresearch.com/reports/widgets",
    source_type: "public_web",
    title: "Widgets market report",
    url: "https://www.kenresearch.com/reports/widgets",
    word_count: 1534,
    ...overrides,
  };
}

export function link(overrides: Partial<ExistingLinkView> = {}): ExistingLinkView {
  return {
    page_id: PAGE_ID,
    page_version: 2,
    source_url: "https://www.kenresearch.com/reports/widgets",
    ordinal: 0,
    anchor_source: "text",
    anchor_text: "Gadgets report",
    block_id: "b3",
    block_kind: "p",
    context_text: "See the Gadgets report.",
    href: "/reports/gadgets",
    link_type: "internal",
    occurrence_index: 0,
    rel: [],
    resolution: "resolved",
    resolved_url: "https://www.kenresearch.com/reports/gadgets",
    target_page_id: OTHER_PAGE_ID,
    ...overrides,
  };
}

export function counters(overrides: Partial<RunCounters> = {}): RunCounters {
  return {
    pages_discovered: 10,
    pages_fetched: 8,
    pages_created: 5,
    pages_changed: 1,
    pages_unchanged: 1,
    pages_duplicate: 0,
    pages_failed: 1,
    pages_skipped: 2,
    pages_gone: 0,
    error_count: 1,
    warning_count: 2,
    ...overrides,
  };
}

export function runSummary(overrides: Partial<IngestionRunSummary> = {}): IngestionRunSummary {
  return {
    id: RUN_ID,
    source_type: "public_web",
    source_name: "https://www.kenresearch.com/",
    status: "completed_with_errors",
    started_at: "2026-10-13T10:00:00Z",
    completed_at: "2026-10-13T10:02:05Z",
    counters: counters(),
    ...overrides,
  };
}

export function runDetail(overrides: Partial<IngestionRunDetail> = {}): IngestionRunDetail {
  return {
    ...runSummary(),
    config_snapshot: {
      base_url: "https://www.kenresearch.com/",
      max_pages: 500,
      allowed_hosts: ["www.kenresearch.com"],
      respect_robots: true,
      api_token: "should-never-show",
      nested: { password: "nope", depth: 3 },
    },
    failure_reason: null,
    issue_counts: { http_not_found: 1, robots_disallowed: 3 },
    ...overrides,
  };
}

export function issue(overrides: Partial<IngestionIssueView> = {}): IngestionIssueView {
  return {
    id: 1,
    run_id: RUN_ID,
    code: "http_not_found",
    severity: "error",
    message: "The URL answered HTTP 404.",
    occurred_at: "2026-10-13T10:01:00Z",
    url: "https://www.kenresearch.com/missing",
    related_url: null,
    http_status: 404,
    ...overrides,
  };
}

export function list<T>(items: T[], page: Partial<PageInfo> = {}): { items: T[]; page: PageInfo } {
  return { items, page: pageInfo({ total: items.length, ...page }) };
}

export function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function errorBody(code: string, requestId: string | null = "req-1") {
  return { code, message: "Something about the request.", details: [], request_id: requestId };
}
