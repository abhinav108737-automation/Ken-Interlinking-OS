import fs from "node:fs/promises";
import path from "node:path";
import { JSDOM } from "jsdom";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const DRAFTS_PATH = path.join(ROOT, "outputs", "contextual-v02-shadow", "new_eligible_interlinking_drafts.json");
const DECISIONS_PATH = path.join(ROOT, "data", "v02-contextual-review", "decision-state.json");
const OUTPUT_DIR = path.join(ROOT, "outputs", "contextual-v02-live");
const EXECUTE = process.argv.includes("--execute");
const ACTOR = "abhinav.kumar";

function parseEnv(text) {
  const result = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const split = line.indexOf("=");
    let value = line.slice(split + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    result[line.slice(0, split).trim()] = value;
  }
  return result;
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function sourceSlug(item) {
  return new URL(item.source_url).pathname.split("/").filter(Boolean).at(-1);
}

function targetPath(item) {
  return new URL(item.destination_url).pathname;
}

function approvedChange(item) {
  const href = targetPath(item);
  const anchorHtml = `<a href="https://thekenconsulting.com${href}">${item.anchor}</a>`;
  const markdownLink = `[${item.anchor}](${item.destination_url})`;
  if (item.after_copy.split(markdownLink).length - 1 !== 1) throw new Error(`${item.id}: approved linked copy is malformed`);
  const afterPlain = item.after_copy.replace(markdownLink, item.anchor);
  if (afterPlain === item.before_copy) {
    const anchorStart = item.before_copy.toLocaleLowerCase().indexOf(item.anchor.toLocaleLowerCase());
    if (anchorStart < 0 || item.before_copy.toLocaleLowerCase().indexOf(item.anchor.toLocaleLowerCase(), anchorStart + item.anchor.length) >= 0) {
      throw new Error(`${item.id}: approved anchor must occur exactly once in approved source copy`);
    }
    const exactAnchor = item.before_copy.slice(anchorStart, anchorStart + item.anchor.length);
    const following = item.before_copy.slice(anchorStart + item.anchor.length).match(/^\s+\w+(?:\s+\w+){0,3}/)?.[0] || "";
    return { find: `${exactAnchor}${following}`, linkSource: exactAnchor, anchorHtml, copyEdit: false };
  }

  let prefix = 0;
  while (prefix < item.before_copy.length && prefix < afterPlain.length && item.before_copy[prefix] === afterPlain[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < item.before_copy.length - prefix && suffix < afterPlain.length - prefix && item.before_copy[item.before_copy.length - 1 - suffix] === afterPlain[afterPlain.length - 1 - suffix]) suffix += 1;
  const oldSegment = item.before_copy.slice(prefix, item.before_copy.length - suffix);
  const newSegment = afterPlain.slice(prefix, afterPlain.length - suffix);
  if (!oldSegment) {
    const geography = item.anchor.split(" ")[0];
    const geographyStart = item.before_copy.indexOf(geography);
    if (geographyStart < 0 || item.before_copy.indexOf(geography, geographyStart + geography.length) >= 0) throw new Error(`${item.id}: approved regional anchor base is ambiguous`);
    const following = item.before_copy.slice(geographyStart + geography.length).match(/^\s+\w+(?:\s+\w+){0,3}/)?.[0] || "";
    return { find: `${geography}${following}`, linkSource: geography, anchorHtml, copyEdit: true };
  }
  if (newSegment !== item.anchor) throw new Error(`${item.id}: approved copy edit is not a single anchor expansion`);
  const following = item.before_copy.slice(item.before_copy.length - suffix).match(/^\s+\w+(?:\s+\w+){0,3}/)?.[0] || "";
  return { find: `${oldSegment}${following}`, linkSource: oldSegment, anchorHtml, copyEdit: true };
}

function countInsensitive(text, needle) {
  return text.toLocaleLowerCase().split(needle.toLocaleLowerCase()).length - 1;
}

async function request(url, options = {}) {
  const response = await fetch(url, { ...options, redirect: "manual", signal: AbortSignal.timeout(45_000) });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { non_json: true }; }
  return { response, data, text };
}

async function main() {
  const env = parseEnv(await fs.readFile(path.join(ROOT, ".env"), "utf8"));
  const base = String(env.KEN_CMS_BASE_URL || "").replace(/\/$/, "");
  const host = new URL(base).hostname;
  if (!["www.thekenconsulting.com", "thekenconsulting.com"].includes(host)) throw new Error("Refusing an unexpected CMS host");
  const token = env.KEN_CMS_WRITE_TOKEN;
  if (!token) throw new Error("KEN_CMS_WRITE_TOKEN is missing");
  const writesEnabled = process.env.KEN_CMS_WRITES_ENABLED ?? env.KEN_CMS_WRITES_ENABLED;
  if (EXECUTE && String(writesEnabled).toLowerCase() !== "true") throw new Error("KEN_CMS_WRITES_ENABLED must be true for --execute");

  const draftsDocument = JSON.parse(await fs.readFile(DRAFTS_PATH, "utf8"));
  const decisionDocument = JSON.parse(await fs.readFile(DECISIONS_PATH, "utf8"));
  const approved = draftsDocument.results.filter((item) => decisionDocument.decisions?.[item.id]?.decision === "approve");
  if (approved.length !== 7) throw new Error(`Expected seven approved new drafts; found ${approved.length}`);

  const headers = { Accept: "application/json", Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  async function validateLink({ pageId, version, anchor, href, occurrence }) {
    const payload = { changes: [{ page_id: pageId, expected_version: version, link: { anchor_text: anchor, href, occurrence } }] };
    const validation = await request(`${base}/internal/link-changes/validate/`, { method: "POST", headers, body: JSON.stringify(payload) });
    const valid = validation.response.ok && validation.data?.invalid === 0 && validation.data?.results?.[0]?.valid === true;
    if (!valid) throw new Error(`link dry-run refused with HTTP ${validation.response.status}; response=${JSON.stringify(validation.data)}`);
  }
  const prepared = [];
  for (const item of approved) {
    if (item.verdict !== "draft_ready") throw new Error(`${item.id}: draft is no longer eligible`);
    const slug = sourceSlug(item);
    const pageId = `${slug}:${item.source_field}`;
    const pageUrl = `${base}/internal/pages/${pageId}/`;
    const pageResult = await request(pageUrl, { headers });
    if (!pageResult.response.ok) throw new Error(`${item.id}: CMS read failed with HTTP ${pageResult.response.status}`);
    const page = pageResult.data;
    const approved = approvedChange(item);
    const href = targetPath(item);
    if (page.protected) throw new Error(`${item.id}: source page is protected`);
    if (page.version !== item.source_version) throw new Error(`${item.id}: approved source version is stale; re-review required`);
    const body = String(page.body_html || "");
    if (!body.toLocaleLowerCase().includes(item.anchor.split(" ")[0].toLocaleLowerCase())) throw new Error(`${item.id}: approved source evidence is not present in the CMS field`);
    const existingHref = new RegExp(`href=["'][^"']*${escapeRegExp(href)}["']`, "i");
    if (existingHref.test(body)) throw new Error(`${item.id}: destination is already linked in this field`);
    const lowerBody = body.toLocaleLowerCase();
    const following = approved.find.slice(approved.linkSource.length);
    const candidateFinds = [approved.find, `<strong>${approved.linkSource}</strong>${following}`];
    let selectedFind = "";
    let phraseIndex = -1;
    for (const candidate of candidateFinds) {
      const lowered = candidate.toLocaleLowerCase();
      const first = lowerBody.indexOf(lowered);
      if (first >= 0 && lowerBody.indexOf(lowered, first + lowered.length) < 0) {
        selectedFind = candidate;
        phraseIndex = first;
        break;
      }
    }
    if (phraseIndex < 0) {
      const contexts = [];
      const probeTerm = item.anchor.split(" ")[0].toLocaleLowerCase();
      let probe = lowerBody.indexOf(probeTerm);
      while (probe >= 0 && contexts.length < 6) {
        contexts.push(body.slice(Math.max(0, probe - 70), probe + 230).replace(/\s+/g, " "));
        probe = lowerBody.indexOf(probeTerm, probe + probeTerm.length);
      }
      throw new Error(`${item.id}: minimal approved phrase is not present exactly once; expected=${JSON.stringify(approved.find)}; cms_contexts=${JSON.stringify(contexts)}`);
    }
    const exactCmsFind = body.slice(phraseIndex, phraseIndex + selectedFind.length);
    const sourceIndex = exactCmsFind.toLocaleLowerCase().indexOf(approved.linkSource.toLocaleLowerCase());
    const occurrence = approved.copyEdit ? 1 : countInsensitive(body.slice(0, phraseIndex), approved.linkSource) + 1;
    let copyChange = null;
    if (approved.copyEdit) {
      const plainNewText = `${exactCmsFind.slice(0, sourceIndex)}${item.anchor}${exactCmsFind.slice(sourceIndex + approved.linkSource.length)}`;
      copyChange = { op: "replace", find: exactCmsFind, new_text: plainNewText };
      const validation = await request(`${pageUrl}content-changes/validate/`, {
        method: "POST", headers, body: JSON.stringify({ expected_version: page.version, change: copyChange }),
      });
      if (!validation.response.ok || validation.data?.valid !== true) throw new Error(`${item.id}: copy dry-run refused with HTTP ${validation.response.status}; response=${JSON.stringify(validation.data)}`);
      console.log(`${item.id}: copy dry-run valid; link dry-run will follow the version-locked copy apply`);
    } else {
      await validateLink({ pageId, version: page.version, anchor: item.anchor, href, occurrence });
      console.log(`${item.id}: link dry-run valid at occurrence ${occurrence}`);
    }
    prepared.push({ item, slug, pageId, pageUrl, href, version: page.version, occurrence, copyChange });
  }

  const report = {
    schema_version: 1,
    mode: EXECUTE ? "live_apply" : "dry_run",
    actor: ACTOR,
    started_at: new Date().toISOString(),
    approved_count: prepared.length,
    results: [],
  };
  if (!EXECUTE) {
    report.results = prepared.map(({ item, pageId, href, version, occurrence, copyChange }) => ({ id: item.id, page_id: pageId, target_path: href, expected_version: version, occurrence, copy_dry_run: copyChange ? "valid" : "not_needed", link_dry_run: copyChange ? "pending_after_copy_apply" : "valid" }));
  } else {
    let priorResults = new Map();
    try {
      const prior = JSON.parse(await fs.readFile(path.join(OUTPUT_DIR, "approved-apply-2026-11-20.json"), "utf8"));
      priorResults = new Map((prior.results || []).map((result) => [result.id, result]));
    } catch { /* No earlier apply record. */ }
    for (const preparedItem of prepared) {
      const { item, slug, pageId, pageUrl, href, version, occurrence, copyChange } = preparedItem;
      const previous = priorResults.get(item.id);
      if (previous?.cms_verified && !previous?.rendered_verified && previous?.rollback_status?.every((entry) => entry.status === "rolled_back")) {
        report.results.push({ ...previous, status: "blocked_public_template", skipped_at: new Date().toISOString() });
        console.log(`${item.id}: skipped; prior CMS canary was rolled back because the anchor is not publicly rendered`);
        continue;
      }
      const operationId = `ken-${item.id.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}-20261120-v2`;
      const record = { id: item.id, page_id: pageId, target_path: href, anchor: item.anchor, operation_id: operationId, expected_version: version, occurrence, deployment_ids: [], rollback_status: null, cms_verified: false, rendered_verified: false };
      try {
        let activeVersion = version;
        if (copyChange) {
          const copied = await request(`${pageUrl}content-changes/`, {
            method: "POST", headers, body: JSON.stringify({ operation_id: `${operationId}-copy`, expected_version: activeVersion, change: copyChange }),
          });
          if (!copied.response.ok || copied.data?.deployment_id == null) throw new Error(`copy apply refused or unknown with HTTP ${copied.response.status}`);
          record.deployment_ids.push(String(copied.data.deployment_id));
          const copiedPage = await request(pageUrl, { headers });
          if (!copiedPage.response.ok || countInsensitive(String(copiedPage.data?.body_html || ""), item.anchor) !== 1) throw new Error("copy verification failed");
          activeVersion = copiedPage.data.version;
          await validateLink({ pageId, version: activeVersion, anchor: item.anchor, href, occurrence: 1 });
        }
        const linked = await request(`${pageUrl}link-changes/`, {
          method: "POST", headers, body: JSON.stringify({ operation_id: `${operationId}-link`, expected_version: activeVersion, link: { anchor_text: item.anchor, href, occurrence: copyChange ? 1 : occurrence } }),
        });
        if (!linked.response.ok || linked.data?.deployment_id == null) throw new Error(`link apply refused or unknown with HTTP ${linked.response.status}`);
        record.deployment_ids.push(String(linked.data.deployment_id));

        const after = await request(pageUrl, { headers });
        const cmsAnchor = new RegExp(`<a[^>]+href=["'][^"']*${escapeRegExp(href)}["'][^>]*>\\s*${escapeRegExp(item.anchor)}\\s*<\\/a>`, "i");
        if (!after.response.ok || !cmsAnchor.test(String(after.data?.body_html || ""))) throw new Error("CMS verification failed");
        record.cms_verified = true;
        record.new_version = after.data.version;

        for (let attempt = 0; attempt < 6; attempt += 1) {
          const refreshed = await request("https://www.thekenconsulting.com/api/revalidate", {
            method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, body: JSON.stringify({ slug, type: "v0.2" }),
          });
          if (!refreshed.response.ok) throw new Error(`public revalidation failed with HTTP ${refreshed.response.status}`);
        }
        const publicPage = await request(item.source_url, { headers: { Accept: "text/html", "Cache-Control": "no-cache" } });
        if (!publicPage.response.ok) throw new Error(`public page failed with HTTP ${publicPage.response.status}`);
        const document = new JSDOM(publicPage.text, { url: item.source_url }).window.document;
        const renderedLinks = [...document.querySelectorAll("a[href]")].filter((link) => {
          const destination = new URL(link.href, item.source_url);
          return destination.pathname === href && link.textContent.trim().replace(/\s+/g, " ") === item.anchor;
        });
        if (renderedLinks.length !== 1) throw new Error(`expected one rendered anchor; found ${renderedLinks.length}`);
        record.rendered_verified = true;
        record.public_url = item.source_url;
        record.completed_at = new Date().toISOString();
        console.log(`${item.id}: applied as deployment(s) ${record.deployment_ids.join(", ")} and publicly verified`);
      } catch (error) {
        record.error = error instanceof Error ? error.message : String(error);
        const rollbackResults = [];
        for (const deploymentId of [...record.deployment_ids].reverse()) {
          const rolledBack = await request(`${base}/internal/deployments/${deploymentId}/rollback/`, { method: "POST", headers });
          rollbackResults.push({ deployment_id: deploymentId, status: rolledBack.response.ok ? "rolled_back" : `failed_http_${rolledBack.response.status}` });
        }
        record.rollback_status = rollbackResults;
        report.results.push(record);
        report.completed_at = new Date().toISOString();
        await fs.mkdir(OUTPUT_DIR, { recursive: true });
        await fs.writeFile(path.join(OUTPUT_DIR, "approved-apply-2026-11-20.json"), `${JSON.stringify(report, null, 2)}\n`, "utf8");
        console.warn(`${item.id}: rolled back after failure: ${record.error}`);
        continue;
      }
      report.results.push(record);
    }
  }

  report.completed_at = new Date().toISOString();
  await fs.mkdir(OUTPUT_DIR, { recursive: true });
  const filename = EXECUTE ? "approved-apply-2026-11-20.json" : "approved-dry-run-2026-11-20.json";
  await fs.writeFile(path.join(OUTPUT_DIR, filename), `${JSON.stringify(report, null, 2)}\n`, "utf8");
  console.log(`${EXECUTE ? "Apply" : "Dry-run"} report: ${path.join("outputs", "contextual-v02-live", filename)}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
