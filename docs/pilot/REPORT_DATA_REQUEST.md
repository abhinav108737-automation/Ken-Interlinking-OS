# Request to the Ken Research technology team: report content for the interlinking pilot

Written for: the Ken Research technology team (CMS / web engineering owner).

**What we are asking for.** A copy of the content of Ken Research's live pages, so the Interlinking OS can propose
contextual internal links between them. This is **read-only**. We are not asking for CMS write access, production
credentials, or permission to publish anything. Google Search Console and GA4 data we already have access to
ourselves, so those are not part of this request.

**Why not just crawl it ourselves.** A crawl sends automated traffic to your production site, which only you can
approve, and it returns page text rather than the CMS's own fields. An export or read-only API gives complete content with
no load on the site. If neither is possible, we ask for approval of a small, throttled crawl instead (option C).

## Options, in order of preference

| | Option | What we need from you |
|---|---|---|
| **A** | **One-off export** (preferred to start) | A file of all live reports, insights, industry and country pages in the format below. A sample of 200-500 pages is enough for the pilot. |
| **B** | **Read-only content API** | Endpoint(s) and a read-only token returning the same fields, with paging and a rate limit we should respect. Production use later. |
| **C** | **Approval of a throttled crawl** | Written approval of `https://www.kenresearch.com/` (host, paths, hours, rate limit), a contact mailbox for the bot, and the egress IP to allow-list. We would start with 25 pages, one request at a time, 3 seconds apart, robots.txt honoured. |

Note: a sitemap lists URLs and last-modified dates only. It has no titles or content, so it cannot replace A or B.

## The data we need for every page

| Field | Why |
|---|---|
| `url` (the public URL) and `canonical_url` | identity; avoids duplicate and alias pages |
| `title`, `meta_description` | topic and anchor wording |
| `body_html`: the **full article body as stored in the CMS** (not the rendered page with header/footer) | the text where links are placed; its structure decides where an anchor is safe |
| `page_type` / template (report, insight, industry hub, country page, sector page, landing, other) | which pages may link to which |
| `publication_state` (published / draft / scheduled / private) | drafts must never be link targets |
| `published_at`, `modified_at` | freshness; detects changed pages |
| `language` | multilingual handling |
| `external_id` (CMS id) | stable identity across renames |
| `metadata` (key/value): author, region, industry, price/licence tier, **protected / do-not-edit flag**, gated or licensed-report flag | protected and licensed pages are excluded automatically |

If the CMS also has categories, tags, related-report fields or industry/country taxonomies, please include them. They
improve relevance and cost nothing to export.

## Format (JSONL, one page per line; the first line is a header)

```json
{"format": "ken-inventory-export", "version": 1, "site_url": "https://www.kenresearch.com/", "allowed_hosts": [], "exported_at": "2026-11-01T00:00:00Z", "generator": "cms-export"}
{"url": "https://www.kenresearch.com/reports/india-ev-market", "external_id": "1234", "page_type": "report", "title": "India EV Market", "meta_description": "...", "publication_state": "published", "language": "en", "canonical_url": "https://www.kenresearch.com/reports/india-ev-market", "published_at": "2025-06-01T09:00:00Z", "modified_at": "2026-10-01T09:00:00Z", "metadata": {"region": "India", "industry": "Automotive", "cms_protected": "false"}, "body_html": "<main>...</main>"}
```

A folder of HTML files (one per page, `reports/india-ev-market.html` -> `/reports/india-ev-market`) also works if JSONL is
awkward. Full specification: `docs/CONTRACTS.md` section 13. A working example: `tests/fixtures/offline_demo/synthetic_extension.jsonl`.

## Scope, volume and handling

* **Scope for the pilot:** published reports, insights and industry / country / sector pages. Please exclude licensed
  report **files**, client-only and gated content, customer data, and anything under embargo; mark borderline pages in
  `metadata` rather than omitting them if you prefer.
* **Volume:** a few hundred pages for the pilot; the full site later. Delivery by encrypted file share or an internal
  link; no email attachments of full content.
* **What happens to it:** it is stored only in a local database and a local vector index on the machine running the
  pilot. It is never sent to an external service or an external model (embeddings and review run on a local open model).
  Real and synthetic content are labelled separately in every report, and nothing is ever published to your CMS from this
  pilot.
* **Retention:** deleted on request; the pilot database can be dropped in one command.

## Questions for you

1. Which option (A, B or C) can you provide, and by when?
2. Is `www.kenresearch.com` the canonical host, and what does `kenresearch.com` do?
3. Which page types and URL patterns exist (report, insight, hub, country, sector, landing), and which are gated or licensed?
4. Is there a "protected" or "do not edit" flag in the CMS?
5. If C: the allowed rate, hours, the bot contact mailbox and the egress IP to allow-list.
