# ADR-0020: Sitemap-seeded crawling for the Ken public-site pilot

Status: accepted (operator direction, 2026-10-27)

## Context
ADR-0009 and the pilot design deliberately gave the crawler no sitemap input, so that a sitemap could never widen the
host, robots or request limits. The operator now wants the pilot to cover URLs listed in a specific sitemap
(`https://www.kenresearch.com/product-sitemap.xml?p=2`) instead of discovering pages by following links, and to render each
page in a headless browser (Playwright, already supported).

## Decision
`CrawlConfig.sitemap_urls` (at most 5; the pilot loader allows at most 3) lets a run name EXACT sitemap files. Each is:
1. checked for scope (approved host) and against `robots.txt` before it is requested;
2. fetched once with the crawler's own guarded, rate-limited client (3 s minimum gap), a body cap (`sitemap_max_bytes`),
   `Accept-Encoding: identity`, no redirect following, and the usual 403/429/5xx back-off;
3. parsed with a regular expression for `<loc>` values (no XML parser, so no entity or DTD expansion);
4. never expanded when it is a sitemap INDEX (a warning is recorded; name the child sitemap instead).

The entries replace the base URL as the seed list. Every seed still passes host scope, include/exclude rules, `robots.txt`
and the page budget; `max_depth` must be 0 so links found on pages are not followed. `Sitemap:` lines in `robots.txt` remain
ignored. A pilot file naming a sitemap has its own configuration hash, so an approval for the link-following pilot does not
cover it, and the live crawl still needs the operator's approval file.

## Consequences
* The operator chooses the exact page population; a sitemap can no longer silently widen it.
* A sitemap fetch is one extra request to the production site, counted in the request ceiling.
* Playwright MCP is not used: the pilot's own Playwright path enforces host, robots, pacing and the SSRF guard on every
  request; a generic browser tool would not.

## Rollback
Remove `sitemap_sources` from the pilot file (the loader and crawler then behave as before). No schema change.
