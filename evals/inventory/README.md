# Inventory fixture corpus (Phase 1)

A deterministic, spec-derived corpus for the Phase 1 content inventory
(ADR-0009). It covers normalisation, file exports and the polite crawler. It has
**no learning behaviour, no skills or policies and no model calls**. Strictly it
is a test suite with golden expectations: the scores are pass/fail. It lives
next to the evals because its goldens follow the same rules: they are versioned
and reviewed, and they are never edited just to make a build pass.

## Run

```bash
.venv/bin/pytest tests/unit/inventory/test_fixture_corpus.py -rA
```

It needs no database, no network and no model server. The crawl runs through an
in-process `httpx.MockTransport` (`tests/support/fixture_site.py`) at
`http://site.fixture.test`. No socket is opened and no real site is contacted.

## Layout

| Path | What it is |
|---|---|
| `tests/fixtures/inventory/site/` | 18 synthetic HTML pages, `robots.txt`, a fake PDF, and `_routes.json` (status codes, redirects, content types, `Content-Language` headers). The pages are host-agnostic: `{{ORIGIN}}` is replaced by the serving origin. |
| `tests/fixtures/inventory/exports/` | `valid.jsonl` (6 records), `v2_changed.jsonl`, `with_errors.jsonl`, `bad_header.jsonl` |
| `tests/fixtures/inventory/expected/` | Goldens: `site_pages.json`, `exports.json`, `crawl.json` |
| `tests/support/fixture_site.py` | Route loader and `MockTransport` factory with a request log |
| `tests/unit/inventory/test_fixture_corpus.py` | The eval |

## What it covers

| Case | Checks (ADR-0009 rule) |
|---|---|
| `/reports/x` | Boilerplate removal: nav, aside, role=navigation, aria-hidden, `hidden`, form, noscript, script, style, and header/footer outside the article. Header and footer inside the article are kept. `<title>` beats og:title. Every link type: internal, external, fragment, `mailto:`/`tel:`/`javascript:`, empty and unparseable. Every anchor source: text, img alt, aria-label, title, none. rel sorted. utm_* stripped. |
| `/reports/alias-x`, `/guides/base-demo` | Declared in-scope canonical. The canonical and the links both resolve against `<base href>`. |
| `/canonical-offsite`, `/canonical-invalid` | Canonical `out_of_scope` and `invalid`. Title falls back to og:title, then to the first h1. |
| `/no-title` | No title source at all, and an invalid `lang` value, so both are null. |
| `/about/`, `/` | Canonical `missing` and `self`. rel is de-duplicated. Protocol-relative and whitespace-padded hrefs. |
| `/contact`, `/reports/sea-ev` | Language precedence: `<html lang>` beats the header, which beats meta http-equiv. Values are lower-cased. Content root is `[role=main]` or `<article>`. External URL normalisation. |
| `/malformed` | Unclosed `<p>`/`<li>`, a stray `</div>`, mis-nested inline tags and unquoted attributes. |
| `/noindex` | robots directives. The page's unique link is never crawled (meta nofollow). |
| Crawl | Expected document set. Each issue code: robots_disallowed, http_not_found, http_gone, redirect_out_of_scope, non_html, limit_reached. The `/old → /older-move → /reports/moved` chain. GET only, nothing off-site, nothing under `/private/`, no utm duplicate. Two crawls yield identical records. |
| Exports | Assertions beat parsed HTML: title, canonical, state, language. Draft, unpublished and unknown states. Dates, metadata, external_id and page_type pass through. A per-record error is an issue, never an exception. A bad header raises. |
| `v2_changed.jsonl` | The hash changes on a body-text change and on a link-target change with the same anchor. It does not change on a whitespace-only change or on a page_type/date/metadata-only change. |
| All pages | Normalising twice gives byte-identical output. The hash ignores fetch facts (time, redirects, depth). A crawled page hashes the same as the same HTML normalised directly. |

## Known tolerances (spec gaps, not bugs)

- **`metadata.title_source` labels are not frozen.** The eval maps
  `og:title` to `og_title` and treats an absent key as `none`.
- **The query-variant cap is ambiguous.** With
  `max_query_variants_per_path=5`, the spec does not say whether the bare
  `/trap` counts as one of the five. The eval accepts 4 or 5 queried variants.
  Today's crawler counts the bare path, so it fetches 4.
- **The redirect chain does not end at `/reports/x`.** It ends at
  `/reports/moved`, a page reachable only by the redirect. Otherwise the chain
  could be unobservable when `/reports/x` was already visited.

## Adding a case

1. Add the page under `site/` and its route to `_routes.json`, or add a JSONL
   record.
2. Write its golden **from ADR-0009 and `ken_contracts/inventory.py`**, before
   running the code. Never paste the implementation's output in as the
   expectation.
3. Run the eval. If it fails, decide from the spec which side is wrong. Fix the
   implementation, or record the corrected golden with the spec rule that
   justifies it.

**The goldens are spec-derived.** Changing an existing expectation is a reviewed
decision. Cite the ADR or contract rule in the change, and never edit a golden
only to make the build pass (AUTONOMY_AND_PERMISSIONS.md: weakening a test is
BLOCK-level). All content is synthetic. No production data and no secrets.
