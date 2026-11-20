import fs from "node:fs/promises";

function parseEnv(text) {
  const values = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const split = line.indexOf("=");
    let value = line.slice(split + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    values[line.slice(0, split).trim()] = value;
  }
  return values;
}

const [slug, fieldName, needle] = process.argv.slice(2);
if (!slug || !fieldName || !needle || !/^[a-z0-9-]+$/.test(slug) || !/^[a-z0-9_]+$/.test(fieldName)) {
  throw new Error("Usage: inspect-evidence.mjs <slug> <field> <needle>");
}
const env = parseEnv(await fs.readFile(".env", "utf8"));
const base = String(env.KEN_CMS_BASE_URL || "").replace(/\/$/, "");
if (!["www.thekenconsulting.com", "thekenconsulting.com"].includes(new URL(base).hostname)) {
  throw new Error("Unexpected CMS host.");
}
const response = await fetch(`${base}/internal/reports/${slug}/`, {
  headers: { Accept: "application/json", Authorization: `Bearer ${env.KEN_CMS_READ_TOKEN}` },
});
if (!response.ok) throw new Error(`CMS read failed (HTTP ${response.status}).`);
const report = await response.json();
const field = report.fields.find((item) => item.field === fieldName);
if (!field) throw new Error(`Field ${fieldName} was not found.`);
const html = String(field.body_html || "");
const lower = html.toLowerCase();
let position = 0;
const matches = [];
while ((position = lower.indexOf(needle.toLowerCase(), position)) >= 0) {
  matches.push(html.slice(Math.max(0, position - 500), Math.min(html.length, position + needle.length + 500)));
  position += needle.length;
}
console.log(JSON.stringify({ slug, field: fieldName, version: field.version, match_count: matches.length, matches }, null, 2));
