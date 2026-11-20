// Read-only: which CMS sections of a report actually appear on its public page?
//
// A CMS write can succeed while the public template never renders that field: three approved
// links (v02-new:1, :2, :4) were applied, found invisible and rolled back for exactly this reason.
// This maps every v0.2 field of a report to the share of its paragraphs that are present in the
// public HTML, so a placement is chosen from sections a reader can see, before any draft is made.
//
//   node tools/v02-contextual-audit/map-rendered-sections.mjs <slug> [keyword ...] [--show=field,field]
//
// Keywords mark visible paragraphs that mention them, as candidate placements; `--show` prints
// every visible paragraph of the named fields, for drafting. Nothing is written.
import fs from "node:fs/promises";
import path from "node:path";
import { JSDOM } from "jsdom";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const args = process.argv.slice(2);
const show = new Set((args.find((arg) => arg.startsWith("--show="))?.slice(7) ?? "").split(",").filter(Boolean));
const [slug, ...keywords] = args.filter((arg) => !arg.startsWith("--"));
if (!slug || !/^[a-z0-9-]+$/.test(slug)) throw new Error("Pass one report slug, then optional keywords.");

function parseEnv(text) {
  return Object.fromEntries(text.split(/\r?\n/).filter((line) => line.trim() && !line.trim().startsWith("#") && line.includes("=")).map((line) => {
    const split = line.indexOf("=");
    return [line.slice(0, split).trim(), line.slice(split + 1).trim().replace(/^['"]|['"]$/g, "")];
  }));
}

const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();

const env = parseEnv(await fs.readFile(path.join(ROOT, ".env"), "utf8"));
const base = String(env.KEN_CMS_BASE_URL || "").replace(/\/$/, "");
if (!["thekenconsulting.com", "www.thekenconsulting.com"].includes(new URL(base).hostname)) throw new Error("Refusing unexpected CMS host");
const headers = { Accept: "application/json", Authorization: `Bearer ${env.KEN_CMS_READ_TOKEN}` };

const report = await (await fetch(`${base}/internal/reports/${slug}/`, { headers, signal: AbortSignal.timeout(45_000) })).json();
const fieldNames = Array.isArray(report.fields)
  ? report.fields.map((field) => (typeof field === "string" ? field : field?.name ?? field?.field)).filter(Boolean)
  : Object.keys(report.fields ?? {});
const v02Fields = fieldNames.filter((name) => name.startsWith("v02_"));

const publicUrl = `https://www.thekenconsulting.com/industry-reports/${slug}`;
const publicHtml = await (await fetch(publicUrl, { headers: { Accept: "text/html", "Cache-Control": "no-cache" }, signal: AbortSignal.timeout(45_000) })).text();
const publicDocument = new JSDOM(publicHtml).window.document;
publicDocument.querySelectorAll("script,style,noscript,template,[hidden],[aria-hidden='true']").forEach((node) => node.remove());
const publicText = normalize(publicDocument.body.textContent);

const sections = [];
for (const field of v02Fields) {
  const page = await (await fetch(`${base}/internal/pages/${slug}:${field}/`, { headers, signal: AbortSignal.timeout(45_000) })).json();
  const document = new JSDOM(String(page.body_html || "")).window.document;
  const paragraphs = [...document.querySelectorAll("p")].map((node) => normalize(node.textContent)).filter((text) => text.split(" ").length >= 12);
  const visible = paragraphs.filter((text) => publicText.includes(text));
  const candidates = keywords.length === 0 ? [] : visible.filter((text) => keywords.some((keyword) => text.toLowerCase().includes(keyword.toLowerCase())));
  sections.push({ field, version: page.version, protected: Boolean(page.protected), paragraphs: paragraphs.length, visible: visible.length, rendered: paragraphs.length > 0 && visible.length === paragraphs.length ? "yes" : visible.length > 0 ? "partly" : "no", candidates, ...(show.has(field) ? { visible_paragraphs: visible } : {}) });
}

console.log(JSON.stringify({ slug, public_url: publicUrl, sections }, null, 2));
