# ADR-0009 — Phase 1 content inventory: sources, identity, versioning

- **Status:** Accepted (Phase 1)
- **Date:** 2026-10-13
- **Phase:** 1

## Context

Phase 1 must establish a reliable canonical page inventory before any recommendation intelligence
exists (IMPLEMENTATION_PLAN.md, PRD §7.1): ingest pages from a source, normalise them
deterministically, persist them with an immutable version history, extract the links they already
contain, and make all of it inspectable.

Three facts shaped the design:

- **No source site is configured.** No governing document names a production base URL, and the
  crawler must never guess one. The crawler is built and tested entirely against local fixtures; a
  real crawl waits for the user to set `KEN_CRAWL_BASE_URL`.
- **The real CMS read API is unknown.** The CMS source must be a working adapter over an interface,
  not an integration.
- **Later phases need stable references.** Phase 3 placements cite a page version, a block and a
  context hash (`LinkLocator`); Phase 6 verifies against a known version. Identity and versions must
  not drift.

## Decision

### 1. One source interface, three implementations

`ContentSourceProvider` (`ken_contracts/inventory.py`) yields `SourceDocument` and `SourceIssue`
records in a deterministic order. Persistence never knows which source produced a document.

| Source | Status in Phase 1 |
|---|---|
| `PublicWebContentSource` | Complete crawler, tested only against local fixture servers |
| `FileExportContentSource` | Complete; the documented JSONL format (`docs/CONTRACTS.md`) |
| `CMSContentSource` | Complete adapter over the read-only `CMSInventoryReader` protocol; only a mock reader exists |

`CMSInventoryReader` is deliberately **separate** from the Phase 0 `CMSProvider`: inventory needs
listing and reading only, so inventory code never holds an object that can mutate content.

A per-URL failure is a `SourceIssue`, never an exception: one bad page cannot fail a run.

### 2. Deterministic normalisation, no LLM

`SourceDocument → NormalizedPage` is a pure function (TRD §11). Same input, byte-identical output.

- **URLs** (`ken_services/inventory/urls.py`) are normalised conservatively: case, default ports,
  dot segments, percent-encoding, fragments and tracking parameters. Trailing slashes, query order,
  `http`/`https` and `www.` are **not** changed, because each can change the resource served. URLs
  with credentials are refused.
- **Parsing** uses BeautifulSoup with Python's built-in `html.parser`: no native code, nothing extra
  to licence-check for a desktop build, and tolerant of malformed markup.
- **Canonical:** the declared `<link rel=canonical>` is used as identity only if it is valid and in
  scope. Otherwise the normalised final URL is used, and `canonical_status` records why.
- **Language:** from `<html lang>` or `Content-Language` only. No detection library.
- **Boilerplate:** removed only where structurally unambiguous (`nav`, `aside`, scripts, forms,
  ARIA landmarks for navigation, banner and contentinfo, and `header`/`footer` outside
  `main`/`article`). No heuristics.

### 3. Identity

`pages.id` is a UUID we mint. `pages.canonical_url` is unique. A source-provided opaque
`external_id` (a CMS page id) is unique when present and **wins over the URL** when matching. A page
whose URL changes in the CMS therefore keeps its id and history. If the external id and the URL point
at two different existing pages, the document is rejected as an `identity_conflict` issue rather than
merged.

### 4. Versioning

- `page_versions` and `existing_links` are **immutable**. A trigger in migration 0002 rejects
  `UPDATE` and `DELETE`, so immutability does not depend on application code.
- A new version is written **only** when `content_hash` changes. The hash covers content fields only:
  - title, canonical, description, language, publication state and robots directives;
  - headings, body text, and every body link (href, anchor, rel).

  It excludes fetch facts such as time, status, redirects and headers. A changed link target with
  unchanged text is a content change, which matters for an interlinking system.
- `hash_version` is stored with each version, so the algorithm can evolve without old hashes looking
  like changes.

### 5. Existing links, resolved at read time

Body links are stored per immutable version, with their block identity:
- `block_id`;
- `block_kind`;
- `occurrence_index`;
- `context_hash` (SHA-256 of the full block text).

These mirror `LinkLocator`. Whether an internal link resolves is computed **at read time** by
joining `resolved_url` to `known_urls`, which records every URL observed, the page it led to, and its
last outcome (page, redirect, 404, 410, robots-blocked …). A stored link row never needs updating.
Redirects and canonical aliases resolve naturally, and "broken" means the destination answered
404/410 or its page is gone.

### 6. Runs, accounting and events

Every crawl or import is an `ingestion_runs` row. It records:
- status;
- timestamps;
- counters, with the invariant `fetched = created + changed + unchanged + duplicate + failed`;
- a **secret-free** configuration snapshot;
- per-URL `ingestion_issues`.

Each document is persisted in its own transaction, together with its events. Events use the
existing vocabulary only:

| Event | When |
|---|---|
| `page.created` | first version of a new page |
| `page.updated` | a new version (content hash changed) |
| `page.deleted` | a known page's identity URL answered 404/410 — an **observation**; nothing is deleted |
| `crawl.completed` | once per run of **any** source type, including failed runs (`status` in the payload) |

No new event name was needed. A file import or CMS read is the same act as a crawl, which is taking
stock of content, so it reuses `crawl.completed` with `source_type` in the payload.

### 7. Read-only API; runs start from the CLI

All nine `/inventory/*` routes are `GET`. Runs start from the command line (`make ingest-file`,
`make crawl`), never over HTTP. There is no authentication before Phase 5, and an unauthenticated
"start a crawl" endpoint would let any caller make this machine fetch URLs.

### 8. Crawler politeness — not configurable away

| Rule | Default / behaviour |
|---|---|
| Scope | base URL's host + explicitly listed hosts; no implied subdomains; off-site URLs never fetched |
| robots.txt | always honoured (no override switch); RFC 9309 longest-match with `*`/`$`; 4xx ⇒ allow all; 5xx/unreachable ⇒ **disallow all**; `Crawl-delay` honoured when larger |
| Rate | 1 s minimum between request starts; concurrency 2 (max 8) |
| Limits | 500 pages, depth 10, 15 s timeout, 5 MB per response, 5 redirects, 10 query variants per path |
| Behaviour | GET only; no cookies stored; no credentials; never submits forms; redirects followed hop-by-hop with each hop scope- and robots-checked |
| Loops | normalised-URL visited set, redirect-loop detection, depth and query-variant caps |

### 9. Rules settled by the pre-fan-out architecture review

- **Canonical precedence.** A document whose canonical is `self` or `missing` is the
  **authority** for its identity URL X. A document that only *declares* X (an alias Y)
  may create identity X only while X has no version from an authoritative document. Once it
  has, the alias writes no version. It records `known_urls(Y → X)` and a `duplicate_canonical`
  issue instead. Within one run, X's own document beats its aliases. Without this rule, X's
  identity would alternate between two contents and gain a new version on every run.
- **Source facts are kept, not dropped.** These are stored per version and excluded from the
  hash:
  - `page_type`: opaque and source-asserted (a CMS template, say), never inferred in Phase 1;
    page classification is Phase 2;
  - `published_at` and `modified_at`;
  - the `Content-Language` header, which is the only response header kept.
- **`known_urls.page_id` is "the last page this URL led to".** A later transient failure
  (robots, timeout) changes the outcome but keeps the mapping, so link resolution does not
  flap. The CHECK is an implication, not an "iff".
- **The current-version pointer is checked by the database.** `pages(id, current_version)`
  references `page_versions` through a `DEFERRABLE INITIALLY DEFERRED` foreign key.
- **Event identity is frozen.**
  - `subject_type` is `page` (`subject_id = str(page_id)`) or `ingestion_run`
    (`subject_id = str(run_id)`).
  - `emitted_by` is `ken-ingest`, and `correlation_id` is the run id.
  - Payloads are always built from the payload models.
- **`page.deleted` fires on the transition only** (`gone_at` NULL → set), and only for 404 or
  410. A page that reappears has `gone_at` cleared, and `page.updated` fires only if its hash
  changed.
- **Identity edge cases.**
  - An alias document's `external_id` is not used for matching, because it names the alias's own
    record, not the canonical page.
  - A document whose URL matches a page that carries a **different** `external_id` is an
    `identity_conflict`. Example: a CMS page deleted and recreated at the same URL under a new id.
    It is recorded and skipped, never merged silently. Resolving it is an operator decision.
- **Link resolution follows the mapping.** A latest outcome of 404 or 410 means `broken`.
  Otherwise, a URL that has ever led to a page resolves to that page, which is `broken` if the
  page is gone. A later timeout or robots block does not make an established link flap to
  `unresolved`.
- **Single writer.** A run holds a session-level `pg_advisory_lock`, so a second concurrent run
  is refused. The lock is released even if the process is killed. Each document's
  transaction takes `SELECT … FOR UPDATE` on its `pages` row.
- **Two small vocabularies, fixed by the fixture evals.**
  - `metadata.title_source` is one of `assertion`, `title`, `og:title` or `h1`, and is absent
    when there is no title.
  - The query-variant cap counts **every** URL sharing a path, including the bare path.
    `/trap` plus `/trap?page=1..4` is 5.
- **Text is quoted exactly as a browser renders it.** Inline elements concatenate (`Hel<b>lo</b>`
  reads "Hello"). Line breaks and block or container boundaries separate words. Phase 3 matches
  anchors against this text and must never fabricate source text (PRD §7.7).
- **`max_pages` caps page requests, whatever their outcome,** so limits stay polite on
  error-heavy sites. robots.txt fetches are not counted.

## Consequences

- Phase 2 adds embeddings and the vector extension on top of `page_versions` without reshaping it.
- Deletions are observed, never inferred. A page missing from a partial crawl is not marked gone;
  only a 404/410 on its identity URL is.
- Moved pages (a 301 from an old URL) get a new page row for the new URL. The old URL's
  `known_urls` entry points at the new page. Merging histories across a move is not attempted in
  Phase 1.
- A run killed with `SIGKILL` stays `running`; it is not reaped automatically (recorded in
  NEXT_STEPS). Its advisory lock is released with the connection, so it does not block the next
  run.
- **Use one authoritative source per site.** The crawler and a file export of the same site
  extract boilerplate differently and assert different metadata. Alternating between them
  produces a new version on every switch.
- `http` and `https` (and `www.` variants) are never unified. A site serving both without
  redirects or canonicals shows duplicate pages. That is surfaced, not hidden.
- The immutability trigger is a row trigger: it does not stop `TRUNCATE` or `DROP TABLE`, which
  only a database owner can run. Integration tests rely on `TRUNCATE`.
- The host IDNA codec is IDNA 2003 (`ß` maps to `ss`), which is documented behaviour.

## Alternatives considered

- **Scrapy.** TRD §2 allows it "where scale warrants". A few hundred polite requests do not, and it
  brings Twisted and its own scheduler.
- **lxml or html5lib.** They are faster or more browser-faithful, but add native code (lxml) or a
  dependency without need. `html.parser` handles the malformed fixtures.
- **Stdlib `urllib.robotparser`.** It does not implement `*` and `$` wildcards or longest-match
  precedence, so it would ignore common `Disallow` patterns. A small RFC 9309 matcher is used
  instead.
- **Storing link resolution.** It would need rows rewritten as other pages are discovered, which
  conflicts with immutable versions. Read-time joins avoid that.
