// Roll back live link deployments on the Ken Consulting test CMS, one at a time, and prove the
// result on the page.
//
//   node tools/v02-contextual-audit/rollback-deployments.mjs --manifest=<file> --report=<new file> [--execute]
//
// The manifest names, per item: `deployment_id`, `page_id` (`slug:field`), `destination_url` and
// a `reason`. For each item the field is read first and must hold exactly one link to the
// destination. After the rollback the CMS read-back must show that link gone, every other link
// untouched and the visible text unchanged (unwrapping a link changes no words); the public page is
// then revalidated and must show no link to the destination. The full field body before and after
// is kept in the report, so any unexpected change can be restored exactly.
//
// Without `--execute` nothing is written: each item is read and checked, and the report records
// what would be rolled back. Writes also require KEN_CMS_WRITES_ENABLED=true.
import fs from "node:fs/promises";
import path from "node:path";
import { JSDOM } from "jsdom";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const option = (name) => process.argv.find((arg) => arg.startsWith(`--${name}=`))?.slice(name.length + 3);
const EXECUTE = process.argv.includes("--execute");
if (!option("manifest") || !option("report")) throw new Error("Pass --manifest=<file> and --report=<new file>");
const MANIFEST_PATH = path.resolve(ROOT, option("manifest"));
const REPORT_PATH = path.resolve(ROOT, option("report"));

function parseEnv(text) {
  return Object.fromEntries(text.split(/\r?\n/).filter((line) => line.trim() && !line.trim().startsWith("#") && line.includes("=")).map((line) => {
    const split = line.indexOf("=");
    return [line.slice(0, split).trim(), line.slice(split + 1).trim().replace(/^['"]|['"]$/g, "")];
  }));
}

const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();

async function request(url, options = {}) {
  const response = await fetch(url, { ...options, redirect: "manual", signal: AbortSignal.timeout(45_000) });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { non_json: true }; }
  return { response, data, text };
}

function linksTo(html, baseUrl, targetPath) {
  const document = new JSDOM(String(html || ""), { url: baseUrl }).window.document;
  const all = [...document.querySelectorAll("a[href]")];
  const matching = all.filter((link) => new URL(link.href, baseUrl).pathname.replace(/\/$/, "") === targetPath.replace(/\/$/, ""));
  const others = all.filter((link) => !matching.includes(link)).map((link) => `${new URL(link.href, baseUrl).pathname}|${normalize(link.textContent)}`).sort();
  return { matching, others, text: normalize(document.body.textContent) };
}

async function main() {
  const existing = await fs.readFile(REPORT_PATH, "utf8").then(JSON.parse, () => null);
  if (existing) throw new Error(`${path.relative(ROOT, REPORT_PATH)} already exists; every run writes a new report`);
  const env = parseEnv(await fs.readFile(path.join(ROOT, ".env"), "utf8"));
  const base = String(env.KEN_CMS_BASE_URL || "").replace(/\/$/, "");
  if (!["thekenconsulting.com", "www.thekenconsulting.com"].includes(new URL(base).hostname)) throw new Error("Refusing unexpected CMS host");
  if (!env.KEN_CMS_WRITE_TOKEN) throw new Error("KEN_CMS_WRITE_TOKEN is missing");
  if (EXECUTE && String(process.env.KEN_CMS_WRITES_ENABLED ?? env.KEN_CMS_WRITES_ENABLED).toLowerCase() !== "true") throw new Error("KEN_CMS_WRITES_ENABLED must be true for execution");
  const headers = { Accept: "application/json", Authorization: `Bearer ${env.KEN_CMS_WRITE_TOKEN}`, "Content-Type": "application/json" };
  const manifest = JSON.parse(await fs.readFile(MANIFEST_PATH, "utf8"));

  const report = { schema_version: 1, mode: EXECUTE ? "live_rollback" : "dry_run", manifest: path.relative(ROOT, MANIFEST_PATH), started_at: new Date().toISOString(), results: [] };
  const save = () => fs.writeFile(REPORT_PATH, `${JSON.stringify(report, null, 2)}\n`, "utf8");

  for (const item of manifest.items) {
    const slug = item.page_id.split(":")[0];
    const sourceUrl = `https://www.thekenconsulting.com/industry-reports/${slug}`;
    const targetPath = new URL(item.destination_url).pathname;
    const pageUrl = `${base}/internal/pages/${item.page_id}/`;
    const entry = { ...item, source_url: sourceUrl, target_path: targetPath, rolled_back: false, cms_verified: false, public_verified: false };
    report.results.push(entry);
    try {
      const before = await request(pageUrl, { headers });
      if (!before.response.ok) throw new Error(`CMS read failed with HTTP ${before.response.status}`);
      const was = linksTo(before.data.body_html, sourceUrl, targetPath);
      if (was.matching.length !== 1) throw new Error(`expected exactly one live link to the destination; found ${was.matching.length}`);
      entry.anchor_text = normalize(was.matching[0].textContent);
      entry.version_before = before.data.version;
      entry.body_before = before.data.body_html;
      await save();
      if (!EXECUTE) {
        console.log(`${item.deployment_id}: would roll back the "${entry.anchor_text}" link in ${item.page_id}`);
        continue;
      }

      const rollback = await request(`${base}/internal/deployments/${item.deployment_id}/rollback/`, { method: "POST", headers });
      entry.rollback_http = rollback.response.status;
      entry.rollback_response = rollback.data;
      if (!rollback.response.ok) throw new Error(`rollback refused with HTTP ${rollback.response.status}: ${JSON.stringify(rollback.data)}`);
      entry.rolled_back = true;
      await save();

      const after = await request(pageUrl, { headers });
      entry.version_after = after.data?.version;
      entry.body_after = after.data?.body_html;
      const now = linksTo(after.data?.body_html, sourceUrl, targetPath);
      if (now.matching.length !== 0) throw new Error(`CMS read-back still holds ${now.matching.length} link(s) to the destination`);
      if (now.text !== was.text) throw new Error("CMS read-back changed the visible text, not only the link");
      if (JSON.stringify(now.others) !== JSON.stringify(was.others)) throw new Error("CMS read-back changed another link");
      entry.cms_verified = true;
      await save();

      for (let attempt = 0; attempt < 6; attempt += 1) {
        const refreshed = await request("https://www.thekenconsulting.com/api/revalidate", { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, body: JSON.stringify({ slug, type: "v0.2" }) });
        if (!refreshed.response.ok) throw new Error(`Public revalidation failed with HTTP ${refreshed.response.status}`);
      }
      const publicPage = await request(sourceUrl, { headers: { Accept: "text/html", "Cache-Control": "no-cache" } });
      const publicDocument = new JSDOM(publicPage.text, { url: sourceUrl }).window.document;
      publicDocument.querySelectorAll("script,style,noscript,template").forEach((node) => node.remove());
      const publicLinks = [...publicDocument.querySelectorAll("a[href]")].filter((link) => new URL(link.href, sourceUrl).pathname.replace(/\/$/, "") === targetPath.replace(/\/$/, "") && normalize(link.textContent).toLowerCase() === entry.anchor_text.toLowerCase());
      if (!publicPage.response.ok || publicLinks.length !== 0) throw new Error(`public page still shows ${publicLinks.length} such link(s)`);
      entry.public_verified = true;
      entry.completed_at = new Date().toISOString();
      await save();
      console.log(`${item.deployment_id}: rolled back; link gone from CMS and public page, text unchanged`);
    } catch (error) {
      entry.error = error instanceof Error ? error.message : String(error);
      await save();
      console.log(`${item.deployment_id}: STOPPED -- ${entry.error}`);
    }
  }
  report.completed_at = new Date().toISOString();
  await save();
}

await main();
