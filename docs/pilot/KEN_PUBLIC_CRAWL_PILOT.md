# Ken Research public-site crawl pilot (read-only)

Status: **PREPARED.** The pilot crawler renders pages with Playwright/Chromium (section 2a). A live run still requires
the operator's signed-off approval file for the exact configuration (section 4). **Incident, 2026-10-26:** while this
browser mode was being added, two unit tests drove the shipped pilot configuration with a mock transport that the
browser ignored, so Chromium made REAL requests to `https://www.kenresearch.com/` (about 65 s of 3-second-spaced
page loads, up to 25 pages, honest user-agent with a placeholder contact, and **without reading the real robots.txt**,
because robots came from the mock). Nothing was stored (unit tests write no database), but it was unapproved live
traffic. Fixed: browser mode now refuses an injected transport and runs only with the SSRF guard, tests that use
mocks force `render: http`, and an autouse fixture blocks any real browser start outside `@pytest.mark.browser` tests,
which use only local loopback sites.

Written for: the operator who will authorise and run the pilot.

## 1. What is prepared

| Item | Location |
|---|---|
| 25-page pilot (active once approved) | `pilots/kenresearch/pilot-25.yaml` |
| 200-500 page pilot (prepared, **`activated: false`**) | `pilots/kenresearch/pilot-large.yaml` (300 pages, depth 4) |
| Loader, hard ceilings, preflight, gated crawl | `services/ken_services/inventory/pilot.py` |
| Crawler (rules, back-off, fail-closed robots) | `services/ken_services/inventory/sources/public_web.py` |
| SSRF / private-network guard | `services/ken_core/net/guard.py` |
| Separate local database creator | `infrastructure/scripts/pilot_db.py` |
| Approval files (never committed) | `pilots/kenresearch/approvals/` (gitignored) |

Limits of the 25-page pilot: 25 page requests, **1 request in flight, at least 3 s between request starts
(robots.txt included)**, depth 3, 20 s timeout, 3 MB per response, 3 redirects, https only, GET only, no
authentication, no cookies, no forms, no CMS writes, no publishing, `KEN_AUTONOMY_MODE=shadow`, `KEN_CMS_PROVIDER=mock`.
The loader refuses any pilot file that breaks these ceilings (concurrency above 1, delay under 3 s, more than 500
pages, http, no robots requirement, no back-off, a non-shadow mode, a wildcard host, a synthetic destination).

## 2. How the crawler protects the site (and you)

* **Hosts.** Only explicitly approved hosts. `www.kenresearch.com` is approved; **`kenresearch.com` (the apex) is a
  separate host and is NOT approved.** A redirect from www to the apex is reported as `redirect_out_of_scope` and not
  followed, so the apex's behaviour is observed (in the run report) before it can ever be crawled. Adding it is a
  reviewed edit of `approved_hosts` once the redirect/canonical behaviour is known. Subdomains are never implied.
* **robots.txt is mandatory and fails closed.** It is fetched first with the same spacing. Only a 2xx answer is used.
  404, 410, 401, 403, 429, 5xx, a timeout, a redirect off the host, an encoded body or a network error all mean the
  origin is treated as **disallowed** and no page is requested (`robots_unavailable`). (The general crawler follows
  RFC 9309 and treats a 404 as "no rules"; the pilot sets `require_robots_file` to be stricter.) `Disallow` and
  `Crawl-delay` are honoured (delay up to 60 s, never below 3 s).
* **Sitemaps cannot bypass anything.** By default the crawler has no sitemap input and discovers by following links
  (depth 3). `pilot-sitemap-25.yaml` (ADR-0020) names ONE exact sitemap file instead: it is fetched once, only after
  `robots.txt` allows it, on an approved host, spaced by the same limiter, with a size cap, no redirect followed and no
  sitemap INDEX expanded. `Sitemap:` lines in robots.txt are still ignored. Its entries become the seed list, and each
  one still passes the host, include/exclude rules, `robots.txt` and the page budget. `max_depth` must be 0 there, so
  links found on pages are never followed.
* **Back-off, no hammering.** Pages are never retried. `Retry-After` on 429/503 (seconds or HTTP date) pauses every
  later request by that long, up to 300 s; a longer value aborts the crawl. Three **consecutive** 403/429/5xx answers
  abort the crawl and no further request is sent. Timeouts and network errors are recorded and not retried.
* **SSRF.** Every URL is resolved first and refused if any address is loopback, private, link-local (including
  169.254.169.254), CGNAT, unique-local, multicast, reserved or a metadata host; every redirect hop is re-checked and
  the connected peer address is verified. Private-network access is a test-only switch that is refused outside
  `local`/`ci` and is off here.
* **Identity.** User-agent: `KenInterlinkBot/0.1 (+mailto:<contact>; read-only content inventory)`. The contact comes
  from the environment variable `KEN_CRAWL_CONTACT` (a **role mailbox or a public https URL, not a personal
  credential**); the preflight blocks on a missing or placeholder value. No cookies, `Authorization` or proxy
  settings are ever sent.
* **Rules (public editorial / market-research content only).** Excluded paths: login, sign-in, register, account,
  user, cart, checkout, basket, order, payment, admin/wp-admin/wp-login/wp-json, search, feeds, xmlrpc, cdn-cgi, and
  download paths. Excluded query parameters: `s q query search keyword keywords sort orderby order filter ref source
  sid sessionid token replytocom` (tracking parameters `utm_*`, `gclid` and similar are stripped by URL
  normalisation). Excluded file types: pdf, office files, archives, csv/xml/json, images, audio/video, css/js.
  **These patterns were written without looking at the live site** (no live request was allowed). Review them
  against the first robots.txt and page sample before widening anything.
* **Eligibility is not assumed.** A crawled page is inventory only. Whether it may ever receive or give a link is
  decided later by the existing eligibility policy, page protections and the shadow-only autonomy engine; nothing is
  published and the mock CMS is the only CMS.
* **Storage is separate from the synthetic corpus.** Pages go to the new local database **`ken_kenresearch_pilot`**
  (port 5433 only) and, later, to Qdrant collection `ken_kenresearch_pilot_pages`. The loader refuses
  `ken_interlink`, `ken_demo`, `ken_cms_sim`, any `*_test` database and the `ken_pages` collection; the preflight
  blocks if the destination already holds pages from any other host (for example `research.example.com`).

## 2a. Browser rendering (Playwright)

`politeness.render: browser` in both pilot files. Chromium is only a **transport**: for every request a page makes
(the document and same-host `script` / `xhr` / `fetch`) the crawler's own gate runs first (https, approved host,
robots.txt, SSRF guard), the request goes through the shared 3-second limiter, and the browser is never allowed to follow
a redirect itself (a 3xx aborts the navigation and the crawler checks the hop). Images, fonts, stylesheets, iframes,
media and third-party code are never loaded; at most 15 same-host subrequests per page; each page gets a fresh context
with no cookies, storage, service workers or downloads; the user-agent stays the honest `KenInterlinkBot` one (no stealth,
no browser impersonation). If the page navigates itself away, that is a failure, not a follow. Limits: the DNS check
replaces the HTTP path's connected-peer check, and a body is buffered before the size cap applies.

Setup (once): `.venv/bin/pip install "playwright==1.64.1"` (or `pip install -e ".[browser]"`), then
`.venv/bin/python -m playwright install chromium`. `make pilot-preflight` blocks if either is missing.
Optional audit images: `--screenshots DIR` on the crawl saves one full-page PNG per page.
Tests: `make test-browser` (loopback fixtures only).

Request ceiling for 25 pages: 25 x (3 redirect hops + 1 + 15 scripts) + 6 robots = **481 requests worst case** (typically
about 26 to 60), at least 78 s and at most 24 minutes of spacing.

## 3. Operator steps up to the approval point (none contacts the site)

```bash
# 1. a contact for site operators (role mailbox or public page; NOT a personal credential)
export KEN_CRAWL_CONTACT='inventory@<your-company-domain>'      # example shape only

# 2. the separate local database (created, migrated, ken_app granted); no site traffic
make pilot-db-create

# 3. offline preflight: shows hosts, robots policy status, sitemap sources, limits, delay/concurrency,
#    include/exclude rules, destination database, autonomy mode, request ceiling and BLOCKERS
make pilot-preflight
```

Exit code 0 means no blockers. The report prints `config_sha256`: the approval file must quote it.

For the 25-page pilot the request ceiling is **481 HTTP requests worst case** in browser mode (section 2a), typically a
few dozen, and at least 78 s of spacing.

## 4. Approval (this is the explicit authorisation step)

Create `pilots/kenresearch/approvals/pilot-25.json` (gitignored). Nothing ships pre-filled:

```json
{
  "config_sha256": "<value printed by make pilot-preflight>",   // 7c9abb1a... for the current browser-mode file
  "scopes": ["live_robots_check", "live_crawl"],
  "hosts": ["www.kenresearch.com"],
  "approved_by": "<name of the person authorising>",
  "expires_at": "<ISO-8601 timestamp, for example 24 hours ahead>"
}
```

An approval is valid only for the exact configuration hash, the exact host list and until it expires; editing the
pilot file invalidates it. Grant `live_robots_check` alone first if you want to see robots.txt before authorising the
crawl.

### 4a. Separate network operation: robots.txt check (NOT EXECUTED)

Fetches **only** `https://www.kenresearch.com/robots.txt` (one request, plus at most five same-host redirects) and
prints availability, crawl-delay and whether the start path is allowed:

```bash
make pilot-robots APPROVAL=pilots/kenresearch/approvals/pilot-25.json     # NOT EXECUTED
```

### 4b. Proposed 25-page crawl (NOT EXECUTED; do not run without authorisation)

```bash
export KEN_CRAWL_CONTACT='inventory@<your-company-domain>'
make pilot-crawl APPROVAL=pilots/kenresearch/approvals/pilot-25.json      # NOT EXECUTED
# which runs:
# caffeinate -dims .venv/bin/python -m ken_services.inventory.pilot crawl \
#     --config pilots/kenresearch/pilot-25.yaml --approval pilots/kenresearch/approvals/pilot-25.json
```

`crawl` re-runs the preflight and refuses (exit 1, reasons on stderr) if there is any blocker, the pilot is not
`activated`, the approval is missing, mismatched or expired, or the contact is unset. It prints one JSON line with the
run id, status and counters.

## 5. After a run (still offline)

1. Read the run summary and the issues: look for `robots_unavailable`, `robots_disallowed`, `redirect_out_of_scope`
   (a www-to-apex redirect would show here), `http_error` with "aborted", `non_html`, `limit_reached`.
2. Inspect `ken_kenresearch_pilot.pages`: hosts, counts, titles. Nothing else is produced: no plans, no decisions.
3. Only then consider `make analyze`/`make qdrant-index` with `KEN_DATABASE_URL` pointed at the pilot database and
   `KEN_QDRANT_COLLECTION=ken_kenresearch_pilot_pages`, still in shadow mode.
4. Undo: `DROP DATABASE ken_kenresearch_pilot` (project container, port 5433) and delete the Qdrant collection.

## 6. The larger pilot (200-500 pages): prepared, not active

`pilots/kenresearch/pilot-large.yaml` has `activated: false`, so the loader marks it as a blocker and `crawl` refuses
it, even with an approval. To use it: review the 25-page results, adjust rules in a reviewed commit, set
`activated: true`, re-run the preflight, write a **new** approval for the new hash. An approval for the 25-page file
never covers it.

## 7. Remaining blockers before a live run

1. **Written authorisation** for the live robots check and the live crawl (the approval file).
2. **`KEN_CRAWL_CONTACT`** set to a monitored role mailbox or public bot-information URL (preflight blocker).
3. **Confirmation that Ken Research permits the crawl** and that the contact above is acceptable to them.
4. robots.txt of `www.kenresearch.com` **has not been read**; its rules, crawl-delay and any bot restrictions are
   unknown. The exclude patterns above are untested against the real site structure.
5. The www/apex redirect and canonical behaviour is **unverified**; the apex stays unapproved until it is.

## Real URL layout and taxonomy v2 (2026-10-27)

The operator's own GA4 and Search Console paths show reports as top-level `-market` slugs, articles under `/articles/`,
corporate pages as `/about-us`, and a `/_next` path. `policies/taxonomy/taxonomy.v2.yaml` classifies these correctly
(page-type rules only; markets and entities are unchanged seed data). It is not the default: select it with
`KEN_TAXONOMY_PATH=policies/taxonomy/taxonomy.v2.yaml` when analysing real content. Both pilot configs now exclude
`/_next/*`, so the approval-template hash was refreshed (`9f3cefb5...`); any approval must be written against the
current hash.

## Sitemap-seeded pilot (2026-10-27, ADR-0020)

`pilots/kenresearch/pilot-sitemap-25.yaml` crawls up to 25 pages taken from
`https://www.kenresearch.com/product-sitemap.xml?p=2` and nothing else (no home page, no link-following), rendering each
in headless Chromium. Its configuration hash is different from `pilot-25.yaml`, so an approval for one never covers the
other. Run it by pointing the Makefile at it:

```
make pilot-preflight PILOT=pilots/kenresearch/pilot-sitemap-25.yaml          # offline
make pilot-robots    PILOT=pilots/kenresearch/pilot-sitemap-25.yaml APPROVAL=pilots/kenresearch/approvals/pilot-sitemap-25.json
make pilot-crawl     PILOT=pilots/kenresearch/pilot-sitemap-25.yaml APPROVAL=pilots/kenresearch/approvals/pilot-sitemap-25.json
```

The approval file needs the scopes `live_robots_check` and `live_crawl`, your name in `approved_by`, the hosts list and an
unexpired `expires_at`; copy `approvals/pilot-sitemap-25.TEMPLATE.json` (invalid until you complete it).
