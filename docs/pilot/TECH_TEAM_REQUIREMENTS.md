# What the Ken Research technology team needs to provide: Interlinking OS pilot

Written for: the Ken Research technology team (engineering, infrastructure, security, CMS and analytics owners).

**Project in one paragraph.** The Interlinking OS reads Ken Research's public pages, finds natural places for internal
links, scores and explains each proposal, checks it with deterministic rules and a local model, and records what it would
publish. It is built and tested end to end on synthetic pages (75 fictional pages, 56 proposals, nothing published).
It has never seen real Ken Research content. Everything runs locally, uses no paid API, and stays in **shadow mode**
(records only, publishes nothing) until the team approves otherwise.

## A. To start a read-only pilot (needed now)

| # | Need | Details / why | Owner |
|---|---|---|---|
| A1 | **Written approval to read the public site**, or a content export instead | Either (a) approve the **25-page pilot crawl** of `https://www.kenresearch.com/`, or (b) hand over a **CMS export** (JSONL in our documented format, or a folder of HTML files) of a representative sample. An export is preferred: zero load on the site. | Tech lead |
| A2 | **Crawl terms if (a)** | Host list (is `www` canonical? is `kenresearch.com` a redirect or a separate site?), allowed paths, any paths to exclude, allowed hours, maximum request rate. Our limits: 1 request at a time, at least 3 s apart, 25 pages, depth 3, GET only, robots.txt required (a missing or forbidden robots.txt stops the crawl). Please also confirm robots.txt rules for the user-agent `KenInterlinkBot`. | Tech lead / security |
| A3 | **Bot identity and allow-listing** | User-agent: `KenInterlinkBot/0.1 (+mailto:<contact>; read-only content inventory)`. We need (i) a **monitored role mailbox or a public bot-information page** for the contact, (ii) the egress IP of the machine that will run it, so it can be allow-listed or rate-exempted in the WAF/CDN, and (iii) the site's **server-side rate limit** so our settings stay well below it. | Security / infra |
| A4 | **JavaScript rendering decision** | Our pilot renders pages in headless Chromium (Playwright). Please confirm that is acceptable, or tell us the pages are server-rendered so plain HTTP fetch is enough. | Tech lead |
| A5 | **Page-type map** | Which URL patterns are market reports, industry hubs, insights, country/sector pages, landing/transactional pages, licensed or gated report pages, client-only pages. Today our exclude rules are guesses (login, cart, search, downloads, PDFs). | CMS / content owner |
| A6 | **Protected-content list** | Pages that must never be edited or receive links (legal, embargoed, client-only, regulated). We honour a CMS "protected" flag and path rules; we need the real flags/rules. | Content owner / legal |
| A7 | **Template samples** | 10-20 real page bodies (HTML as stored in the CMS, not rendered) so we can check where anchors may be placed safely (widgets, tables, headings, existing link density). | CMS owner |

## B. To measure SEO effect (needed before any effectiveness claim)

| # | Need | Details |
|---|---|---|
| B1 | **Google Search Console** read access (property `sc-domain:` or URL-prefix) | A **read-only** OAuth token or service account for query/page performance. Per-page clicks, impressions, position for the pilot pages, for the last 16 months. |
| B2 | **GA4** read access | Property id, its time zone, a read-only token. Optional: register the custom dimension `ken_link_id` and the two link-click events so link-level clicks can be measured. |
| B3 | **Consent / legal decision** on the first-party click beacon | It sends behavioural events and sets no cookie; ePrivacy, GDPR and India's DPDP Act may still apply. Legal sign-off needed before it is served from a real page. |
| B4 | **Baseline period and control group** | Agreement on a pre-registered metric, a baseline window, and a **holdout set of pages that will not receive links** (the system supports a gated holdout; it is off by default). |
| B5 | **Server logs or crawl stats** (optional) | Googlebot crawl frequency for pilot pages, before and after. |

## C. To publish links (later; each item is a separate approval)

| # | Need | Details |
|---|---|---|
| C1 | **CMS API contract** | Base URL, link-change endpoints, how a version conflict (409) differs from a reused operation id, rate limits, page-version/ETag semantics, a rollback/revert call. We have a typed adapter and a simulator, not the real mapping. |
| C2 | **A staging CMS** with copies of real pages | All write testing happens there first, never in production. |
| C3 | **A scoped service credential** | Write access limited to the pilot pages and the link-insertion operation only; stored in your secret manager, never in the repository. Production write requires its own written approval. |
| C4 | **Audit and rollback requirements** | Where your change log lives, who may revert, the maximum time an erroneous link may stay live. |
| C5 | **Operator accounts and approval policy** | Which named people may switch the system from shadow to guarded/autonomous publishing. Both gated modes need explicit acknowledgements and are **off** today. |
| C6 | **Editorial rules** | Maximum links per page, banned anchors, brand/legal wording, languages and geographies in scope. |

## D. Hosting and operations (for anything beyond a laptop)

| # | Need | Details |
|---|---|---|
| D1 | **A host** | Linux or macOS server: about 16 GB RAM minimum for the local 8B model, Apple-silicon or a GPU recommended; 50 GB disk. Runs PostgreSQL 16, Qdrant (self-hosted), Ollama, the API and the web app. Nothing is sent to an external service. |
| D2 | **PostgreSQL** | A managed or self-hosted instance with two roles: an owner for migrations and the restricted `ken_app` for the runtime (built; its password must come from your secret manager). Backups and a restore test. |
| D3 | **Network and TLS** | Internal-only access to the dashboard, HTTPS in front of it, an identity provider if you want single sign-on (today: local accounts with lock-out and sessions). |
| D4 | **Secrets management** | Where database passwords, the CMS token and analytics tokens live, and how they are rotated. |
| D5 | **Security review** | A review of the SSRF guard, the crawler, the role model and the dependency list (all free/open-source; Playwright and Chromium are the newest additions). |
| D6 | **Model policy** | Confirmation that local open models (`qwen3:8b`, `nomic-embed-text`, Apache-2.0) are acceptable, and that no content may leave the environment. A smaller model was tested and rejected on quality. |

## E. People and decisions

* One **technical owner** and one **content/SEO owner** who can label a few hundred proposals as accept / reject / wrong
  anchor. This is the only way to measure real precision and calibrate the thresholds (currently uncalibrated by design).
* A decision on **pilot scope**: page types, geographies, and the success metric.
* Time estimate for the first loop: approval, 25-page crawl and review (days), labelled sample (about a week), staging
  CMS test (about a week), controlled live rollout with a holdout (weeks of data before any conclusion).

## F. What we will and will not do (so you can approve with confidence)

* Read-only on the website; no forms, login, cookies or downloads; robots.txt is mandatory; throttled and self-aborting on
  403/429/5xx; honest bot identity with a contact; SSRF and private-network protection.
* Shadow mode by default; no publishing without C1-C5 and a separate written approval.
* Nothing synthetic is ever presented as a Ken Research result; real and synthetic findings are labelled apart.

## G. Disclosure

During development, two unit tests accidentally drove the pilot configuration against `https://www.kenresearch.com/`
through the new headless-browser mode: about a minute of page requests, spaced 3 seconds apart, honest user-agent with a
placeholder contact, **without reading the real robots.txt**, nothing stored. It happened from this development machine on
the evening of 2026-10-26. It is fixed and regression-tested (docs/pilot/KEN_PUBLIC_CRAWL_PILOT.md, "Incident"). Please
check your logs if you want to confirm; we can supply the user-agent string.
