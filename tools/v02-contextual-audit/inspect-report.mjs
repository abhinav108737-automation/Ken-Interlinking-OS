import fs from "node:fs/promises";

function parseEnv(text) {
  const out = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const index = line.indexOf("=");
    let value = line.slice(index + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    out[line.slice(0, index).trim()] = value;
  }
  return out;
}

const slug = process.argv[2];
if (!slug || !/^[a-z0-9-]+$/.test(slug)) throw new Error("Pass one report slug.");
const env = parseEnv(await fs.readFile(".env", "utf8"));
const base = String(env.KEN_CMS_BASE_URL || "").replace(/\/$/, "");
const url = new URL(base);
if (!["www.thekenconsulting.com", "thekenconsulting.com"].includes(url.hostname)) throw new Error("Unexpected CMS host.");
const response = await fetch(`${base}/internal/reports/${slug}/`, { headers: { Accept: "application/json", Authorization: `Bearer ${env.KEN_CMS_READ_TOKEN}` } });
const data = await response.json();
console.log(JSON.stringify({ status: response.status, keys: Object.keys(data), fields_kind: Array.isArray(data.fields) ? "array" : typeof data.fields, field_keys: data.fields && !Array.isArray(data.fields) && typeof data.fields === "object" ? Object.keys(data.fields) : null, fields_sample: Array.isArray(data.fields) ? data.fields.slice(0, 4).map((field) => typeof field === "object" && field ? Object.keys(field) : typeof field) : null }, null, 2));
if (!response.ok) process.exitCode = 1;
