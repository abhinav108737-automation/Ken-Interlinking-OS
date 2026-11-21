// Rewrite the copy of the live cluster links (deployments 43-54) to the owner's revised wording.
//
// Each live sentence reads `The <a href="…">Anchor</a> <tail>`. Only the plain-text tail after the
// link is replaced, so the link itself is never rewritten and `find` carries no markup. Every
// rewrite must start with the same `The <Anchor> ` prefix; the rest is checked against the owner's
// rule (two or more sentences, 32-38 words, no dashes, no reader-address rationale) BEFORE any
// request is made.
//
// Gate chain per page (ADR-0030): read -> protected? -> old tail present exactly once -> the link
// present exactly once -> validate replace (dry run) -> apply under an operation_id -> CMS
// read-back -> public revalidation -> rendered-DOM check. Any failure after the write rolls back
// that one deployment. Without `--execute` nothing is written.
import fs from "node:fs/promises";
import path from "node:path";
import { JSDOM } from "jsdom";
import { copyRuleProblems } from "./copy-rules.mjs";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const LIVE_PATH = path.join(ROOT, "outputs", "contextual-v02-live", "approved-cluster-batch-2026-11-20.json");
const MANIFEST_PATH = path.join(ROOT, "outputs", "contextual-v02-live", "cluster-copy-rewrite-2026-11-20.json");
const REPORT_PATH = path.join(ROOT, "outputs", "contextual-v02-live", "cluster-copy-rewrite-apply-2026-11-20.json");
const EXECUTE = process.argv.includes("--execute");

function parseEnv(text) {
  return Object.fromEntries(text.split(/\r?\n/).filter((line) => line.trim() && !line.trim().startsWith("#") && line.includes("=")).map((line) => {
    const split = line.indexOf("=");
    return [line.slice(0, split).trim(), line.slice(split + 1).trim().replace(/^['"]|['"]$/g, "")];
  }));
}

const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
const count = (haystack, needle) => haystack.split(needle).length - 1;

async function request(url, options = {}) {
  const response = await fetch(url, { ...options, redirect: "manual", signal: AbortSignal.timeout(45_000) });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { non_json: true }; }
  return { response, data, text };
}

async function save(report) {
  await fs.writeFile(REPORT_PATH, `${JSON.stringify(report, null, 2)}\n`, "utf8");
}

async function main() {
  const env = parseEnv(await fs.readFile(path.join(ROOT, ".env"), "utf8"));
  const base = String(env.KEN_CMS_BASE_URL || "").replace(/\/$/, "");
  if (!["thekenconsulting.com", "www.thekenconsulting.com"].includes(new URL(base).hostname)) throw new Error("Refusing unexpected CMS host");
  if (!env.KEN_CMS_WRITE_TOKEN) throw new Error("KEN_CMS_WRITE_TOKEN is missing");
  if (EXECUTE && String(process.env.KEN_CMS_WRITES_ENABLED ?? env.KEN_CMS_WRITES_ENABLED).toLowerCase() !== "true") throw new Error("KEN_CMS_WRITES_ENABLED must be true for execution");
  const headers = { Accept: "application/json", Authorization: `Bearer ${env.KEN_CMS_WRITE_TOKEN}`, "Content-Type": "application/json" };
  const live = JSON.parse(await fs.readFile(LIVE_PATH, "utf8"));
  const manifest = JSON.parse(await fs.readFile(MANIFEST_PATH, "utf8"));

  // Every rewrite is checked before any network call: a bad manifest must not reach the CMS.
  const plan = [];
  for (const [id, after] of Object.entries(manifest.rewrites)) {
    const record = live.results.find((result) => result.id === id);
    if (!record?.rendered_verified) throw new Error(`${id}: not a live, verified cluster link`);
    const prefix = `The ${record.anchor} `;
    if (!record.proposed_sentence.startsWith(prefix)) throw new Error(`${id}: live sentence does not start with "${prefix}"`);
    if (!after.startsWith(prefix)) throw new Error(`${id}: rewrite must keep the opening "${prefix}"`);
    const problems = copyRuleProblems(after);
    if (problems.length) throw new Error(`${id}: rewrite breaks the owner's rule: ${problems.join("; ")}`);
    plan.push({ id, record, before: record.proposed_sentence, after, oldTail: record.proposed_sentence.slice(prefix.length), newTail: after.slice(prefix.length) });
  }

  const report = { schema_version: 1, mode: EXECUTE ? "live_copy_rewrite" : "dry_run", started_at: new Date().toISOString(), results: [] };
  const prepared = [];
  for (const item of plan) {
    const { id, record } = item;
    const pageUrl = `${base}/internal/pages/${record.page_id}/`;
    const entry = { id, page_id: record.page_id, link_deployment_id: record.deployment_id, before: item.before, after: item.after, deployment_id: null, cms_verified: false, rendered_verified: false, rollback_status: null };
    report.results.push(entry);
    try {
      const pageResult = await request(pageUrl, { headers });
      if (!pageResult.response.ok) throw new Error(`CMS read failed with HTTP ${pageResult.response.status}`);
      const page = pageResult.data;
      if (page.protected) throw new Error("source field is protected");
      const body = String(page.body_html || "");
      if (count(body, item.oldTail) !== 1) throw new Error(`expected the live tail exactly once; found ${count(body, item.oldTail)}`);
      const document = new JSDOM(body).window.document;
      const links = [...document.querySelectorAll("a[href]")].filter((link) => new URL(link.href, record.source_url).pathname === record.target_path);
      if (links.length !== 1) throw new Error(`expected the live link exactly once; found ${links.length}`);
      const change = { op: "replace", find: item.oldTail, new_text: item.newTail };
      const validation = await request(`${pageUrl}content-changes/validate/`, { method: "POST", headers, body: JSON.stringify({ expected_version: page.version, change }) });
      if (!validation.response.ok || validation.data?.valid !== true) throw new Error(`dry run failed with HTTP ${validation.response.status}: ${JSON.stringify(validation.data)}`);
      entry.expected_version = page.version;
      entry.validation = "valid";
      prepared.push({ ...item, entry, pageUrl, change, version: page.version });
      console.log(`${id}: dry-run valid`);
    } catch (error) {
      entry.error = error instanceof Error ? error.message : String(error);
      console.log(`${id}: REFUSED -- ${entry.error}`);
    }
  }
  await save(report);
  if (!EXECUTE) {
    report.completed_at = new Date().toISOString();
    await save(report);
    console.log(`Validated ${prepared.length} of ${plan.length} rewrites; no CMS writes performed`);
    return;
  }

  for (const item of prepared) {
    const { entry, record } = item;
    try {
      entry.operation_id = `ken-${item.id.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}-copy-20261120-v1`;
      entry.apply_started_at = new Date().toISOString();
      await save(report);
      const applied = await request(`${item.pageUrl}content-changes/`, { method: "POST", headers, body: JSON.stringify({ operation_id: entry.operation_id, expected_version: item.version, change: item.change }) });
      if (!applied.response.ok || applied.data?.deployment_id == null) throw new Error(`Apply failed or was ambiguous with HTTP ${applied.response.status}`);
      entry.deployment_id = String(applied.data.deployment_id);
      await save(report);

      const after = await request(item.pageUrl, { headers });
      const afterBody = String(after.data?.body_html || "");
      const afterDocument = new JSDOM(afterBody).window.document;
      const afterText = normalize(afterDocument.body.textContent);
      const afterLinks = [...afterDocument.querySelectorAll("a[href]")].filter((link) => new URL(link.href, record.source_url).pathname === record.target_path && normalize(link.textContent) === record.anchor);
      if (!after.response.ok || afterLinks.length !== 1 || !afterText.includes(item.after) || afterText.includes(normalize(item.oldTail))) throw new Error("CMS read-back did not show exactly the new sentence with its link");
      entry.cms_verified = true;
      entry.final_version = after.data.version;
      await save(report);

      for (let attempt = 0; attempt < 6; attempt += 1) {
        const refreshed = await request("https://www.thekenconsulting.com/api/revalidate", { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, body: JSON.stringify({ slug: record.page_id.split(":")[0], type: "v0.2" }) });
        if (!refreshed.response.ok) throw new Error(`Public revalidation failed with HTTP ${refreshed.response.status}`);
      }
      const publicPage = await request(record.source_url, { headers: { Accept: "text/html", "Cache-Control": "no-cache" } });
      const publicDocument = new JSDOM(publicPage.text, { url: record.source_url }).window.document;
      publicDocument.querySelectorAll("script,style,noscript,template,[hidden],[aria-hidden='true']").forEach((node) => node.remove());
      const publicText = normalize(publicDocument.body.textContent);
      const publicLinks = [...publicDocument.querySelectorAll("a[href]")].filter((link) => new URL(link.href, record.source_url).pathname === record.target_path && normalize(link.textContent) === record.anchor);
      if (!publicPage.response.ok || publicLinks.length !== 1 || !publicText.includes(item.after) || publicText.includes(normalize(item.oldTail))) throw new Error(`Public verification failed; visible anchors=${publicLinks.length}`);
      entry.rendered_verified = true;
      entry.completed_at = new Date().toISOString();
      await save(report);
      console.log(`${item.id}: rewritten and publicly verified; deployment ${entry.deployment_id}`);
    } catch (error) {
      entry.error = error instanceof Error ? error.message : String(error);
      if (entry.deployment_id) {
        const rollback = await request(`${base}/internal/deployments/${entry.deployment_id}/rollback/`, { method: "POST", headers });
        entry.rollback_status = rollback.response.ok ? "rolled_back" : `failed_http_${rollback.response.status}`;
      }
      entry.failed_at = new Date().toISOString();
      await save(report);
      console.log(`${item.id}: FAILED -- ${entry.error}; rollback ${entry.rollback_status ?? "not needed"}`);
    }
  }
  report.completed_at = new Date().toISOString();
  await save(report);
  console.log(`\n${report.results.filter((result) => result.rendered_verified).length} of ${prepared.length} rewrites are live and publicly verified`);
}

await main();
