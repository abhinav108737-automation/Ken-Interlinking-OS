import fs from "node:fs/promises";
import path from "node:path";
import { JSDOM } from "jsdom";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const CSV_PATH = path.join(ROOT, "data", "v02-contextual-review", "v02_reports_test_interlinking.csv");
const OUTPUT_PATH = path.join(ROOT, "outputs", "contextual-v02-shadow", "exact-market-cluster-audit.json");
const ALLOWED_FIELDS = new Set(["v02_growth_drivers", "v02_market_trajectory", "v02_market_segmentation", "v02_regional_analysis"]);

function parseEnv(text) {
  const values = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const split = line.indexOf("=");
    let value = line.slice(split + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    values[line.slice(0, split).trim()] = value;
  }
  return values;
}

function parseCsv(text) {
  const rows = [];
  let row = [], cell = "", quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (quoted) {
      if (character === '"' && text[index + 1] === '"') { cell += '"'; index += 1; }
      else if (character === '"') quoted = false;
      else cell += character;
    } else if (character === '"') quoted = true;
    else if (character === ",") { row.push(cell); cell = ""; }
    else if (character === "\n") { row.push(cell.replace(/\r$/, "")); rows.push(row); row = []; cell = ""; }
    else cell += character;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  const headers = rows.shift();
  return rows.filter((values) => values.some(Boolean)).map((values) => Object.fromEntries(headers.map((header, index) => [header, values[index] || ""])));
}

function normalize(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function visibleBlocks(field) {
  const document = new JSDOM(String(field.body_html || "")).window.document;
  return [...document.querySelectorAll("p, li")]
    .filter((element) => !element.closest("table"))
    .map((element) => normalize(element.textContent))
    .filter((text) => text.split(/\s+/).length >= 12);
}

async function main() {
  const rows = parseCsv(await fs.readFile(CSV_PATH, "utf8"));
  const grouped = Map.groupBy(rows.filter((row) => row.market_core), (row) => row.market_core.trim().toLocaleLowerCase());
  const clusters = [...grouped.entries()]
    .filter(([, reports]) => reports.length > 1 && new Set(reports.map((report) => report.geography)).size > 1)
    .map(([marketCore, reports]) => ({ market_core: marketCore, reports }));
  const env = parseEnv(await fs.readFile(path.join(ROOT, ".env"), "utf8"));
  const base = String(env.KEN_CMS_BASE_URL || "").replace(/\/$/, "");
  if (!["www.thekenconsulting.com", "thekenconsulting.com"].includes(new URL(base).hostname)) throw new Error("Unexpected CMS host");
  if (!env.KEN_CMS_READ_TOKEN) throw new Error("CMS read token is missing");
  const headers = { Accept: "application/json", Authorization: `Bearer ${env.KEN_CMS_READ_TOKEN}` };
  const uniqueReports = [...new Map(clusters.flatMap((cluster) => cluster.reports).map((report) => [report.slug, report])).values()];
  const evidenceBySlug = new Map();
  for (const report of uniqueReports) {
    const [cmsResponse, publicResponse] = await Promise.all([
      fetch(`${base}/internal/reports/${report.slug}/`, { headers, signal: AbortSignal.timeout(45_000) }),
      fetch(report.url, { headers: { Accept: "text/html", "Cache-Control": "no-cache" }, signal: AbortSignal.timeout(45_000) }),
    ]);
    if (!cmsResponse.ok) throw new Error(`${report.slug}: CMS read failed with HTTP ${cmsResponse.status}`);
    const cms = await cmsResponse.json();
    const publicText = publicResponse.ok
      ? normalize(new JSDOM(await publicResponse.text(), { url: report.url }).window.document.body.textContent).toLocaleLowerCase()
      : "";
    const fields = (cms.fields || [])
      .filter((field) => ALLOWED_FIELDS.has(field.field))
      .map((field) => ({
        field: field.field,
        version: field.version,
        links: [...new JSDOM(String(field.body_html || "")).window.document.querySelectorAll("a[href]")].map((link) => new URL(link.href, report.url).pathname),
        blocks: visibleBlocks(field).map((text) => ({ text, public_visible: publicText.includes(normalize(text).toLocaleLowerCase()) })),
      }));
    evidenceBySlug.set(report.slug, { protected: cms.protected, public_status: publicResponse.status, fields });
    console.log(`${report.slug}: fetched (public ${publicResponse.status})`);
  }
  const result = {
    schema_version: 1,
    mode: "read_only_cluster_audit",
    generated_at: new Date().toISOString(),
    inventory_reports: rows.length,
    exact_market_clusters: clusters.length,
    clusters: clusters.map((cluster) => ({
      ...cluster,
      reports: cluster.reports.map((report) => ({ ...report, evidence: evidenceBySlug.get(report.slug) })),
    })),
  };
  await fs.mkdir(path.dirname(OUTPUT_PATH), { recursive: true });
  await fs.writeFile(OUTPUT_PATH, `${JSON.stringify(result, null, 2)}\n`, "utf8");
  console.log(`Audit written: ${path.relative(ROOT, OUTPUT_PATH)}`);
}

main().catch((error) => { console.error(error instanceof Error ? error.message : String(error)); process.exitCode = 1; });
