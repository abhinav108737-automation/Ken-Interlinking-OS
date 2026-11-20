import fs from "node:fs/promises";
import path from "node:path";
import { JSDOM } from "jsdom";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const REPORT_PATH = path.join(ROOT, "outputs", "contextual-v02-live", "topic-kuwait-1-live-2026-11-20.json");
const EVIDENCE_PATH = path.join(ROOT, "outputs", "contextual-v02-shadow", "kuwait_topic_evidence.json");
const EXECUTE = process.argv.includes("--execute");
const SOURCE_SLUG = "kuwait-cloud-based-retail-inventory-forecasting-platforms-market";
const TARGET_SLUG = "kuwait-cloud-based-order-management-systems-market";
const FIELD = "v02_market_trajectory";
const ANCHOR = "order management systems";
const TARGET_PATH = `/industry-reports/${TARGET_SLUG}`;
const OLD_TAIL = "—a market projected to reach USD 137 Mn by 2032—coordinate stock reservations and fulfilment against forecast-led inventory plans.";
const NEW_TAIL = " help turn inventory forecasts into stock reservations and fulfilment plans. The Kuwait market is projected to reach USD 137 Mn by 2032.";
const NEW_SENTENCE = `For execution, ${ANCHOR}${NEW_TAIL}`;
const TARGET_FACT = "The Kuwait Cloud-Based Order Management Systems Market reaches USD 137 Mn in 2032 at a 13.9% revenue CAGR for 2025–2032.";

function parseEnv(text) {
  return Object.fromEntries(text.split(/\r?\n/).filter((line) => line.trim() && !line.trim().startsWith("#") && line.includes("=")).map((line) => {
    const split = line.indexOf("=");
    return [line.slice(0, split).trim(), line.slice(split + 1).trim().replace(/^['"]|['"]$/g, "")];
  }));
}

const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

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
  const report = JSON.parse(await fs.readFile(REPORT_PATH, "utf8"));
  if (report.id !== "topic-kuwait:1" || report.rendered_verified !== true) throw new Error("The original canary is not live and verified");
  const evidence = JSON.parse(await fs.readFile(EVIDENCE_PATH, "utf8"));
  const target = evidence.reports.find((item) => item.slug === TARGET_SLUG);
  const supported = target?.fields?.flatMap((field) => field.blocks || []).some((block) => block.public_visible === true && normalize(block.text).includes(normalize(TARGET_FACT)));
  if (!supported) throw new Error("The proposed destination statistic is not supported in current public target evidence");

  const env = parseEnv(await fs.readFile(path.join(ROOT, ".env"), "utf8"));
  const base = String(env.KEN_CMS_BASE_URL || "").replace(/\/$/, "");
  if (!["thekenconsulting.com", "www.thekenconsulting.com"].includes(new URL(base).hostname)) throw new Error("Refusing unexpected CMS host");
  if (!env.KEN_CMS_WRITE_TOKEN) throw new Error("KEN_CMS_WRITE_TOKEN is missing");
  if (EXECUTE && String(process.env.KEN_CMS_WRITES_ENABLED ?? env.KEN_CMS_WRITES_ENABLED).toLowerCase() !== "true") throw new Error("KEN_CMS_WRITES_ENABLED must be true for execution");
  const headers = { Accept: "application/json", Authorization: `Bearer ${env.KEN_CMS_WRITE_TOKEN}`, "Content-Type": "application/json" };
  const pageId = `${SOURCE_SLUG}:${FIELD}`;
  const pageUrl = `${base}/internal/pages/${pageId}/`;
  const before = await request(pageUrl, { headers });
  if (!before.response.ok) throw new Error(`CMS read failed with HTTP ${before.response.status}`);
  const body = String(before.data?.body_html || "");
  const currentAnchor = new RegExp(`<a[^>]+href=["'][^"']*${escapeRegExp(TARGET_PATH)}["'][^>]*>\\s*${escapeRegExp(ANCHOR)}\\s*<\\/a>`, "i");
  if (!currentAnchor.test(body)) throw new Error("The existing approved anchor is no longer present");
  if (body.split(OLD_TAIL).length - 1 !== 1) throw new Error("The existing sentence tail is no longer unique");
  if (body.includes(NEW_TAIL)) throw new Error("The destination-data revision is already present");
  const change = { op: "replace", find: OLD_TAIL, new_text: NEW_TAIL };
  const validation = await request(`${pageUrl}content-changes/validate/`, { method: "POST", headers, body: JSON.stringify({ expected_version: before.data.version, change }) });
  if (!validation.response.ok || validation.data?.valid !== true) throw new Error(`Revision dry run failed with HTTP ${validation.response.status}: ${JSON.stringify(validation.data)}`);
  if (!EXECUTE) {
    console.log("topic-kuwait:1 destination-data revision dry-run valid; no CMS writes performed");
    return;
  }

  const revision = {
    operation_id: "ken-topic-kuwait-1-human-copy-20261120-v2",
    expected_version: before.data.version,
    old_sentence: `For execution, ${ANCHOR}${OLD_TAIL}`,
    new_sentence: NEW_SENTENCE,
    target_evidence: TARGET_FACT,
    validation: "valid",
    deployment_id: null,
    cms_verified: false,
    rendered_verified: false,
    rollback_status: null,
    started_at: new Date().toISOString(),
  };
  report.revisions = [...(report.revisions || []), revision];
  await save(report);
  try {
    const applied = await request(`${pageUrl}content-changes/`, { method: "POST", headers, body: JSON.stringify({ operation_id: revision.operation_id, expected_version: before.data.version, change }) });
    if (!applied.response.ok || applied.data?.deployment_id == null) throw new Error(`Revision apply failed or was ambiguous with HTTP ${applied.response.status}`);
    revision.deployment_id = String(applied.data.deployment_id);
    report.deployment_ids.push(revision.deployment_id);
    await save(report);

    const after = await request(pageUrl, { headers });
    const afterDocument = new JSDOM(String(after.data?.body_html || "")).window.document;
    if (!after.response.ok || !currentAnchor.test(String(after.data?.body_html || "")) || !normalize(afterDocument.body.textContent).includes(normalize(NEW_SENTENCE))) throw new Error("Revised linked sentence was not verified in CMS content");
    revision.cms_verified = true;
    revision.new_version = after.data.version;
    await save(report);

    for (let attempt = 0; attempt < 6; attempt += 1) {
      const refreshed = await request("https://www.thekenconsulting.com/api/revalidate", { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, body: JSON.stringify({ slug: SOURCE_SLUG, type: "v0.2" }) });
      if (!refreshed.response.ok) throw new Error(`Public revalidation failed with HTTP ${refreshed.response.status}`);
    }
    const publicPage = await request(`https://www.thekenconsulting.com/industry-reports/${SOURCE_SLUG}`, { headers: { Accept: "text/html", "Cache-Control": "no-cache" } });
    const publicDocument = new JSDOM(publicPage.text, { url: `https://www.thekenconsulting.com/industry-reports/${SOURCE_SLUG}` }).window.document;
    publicDocument.querySelectorAll("script,style,noscript,template,[hidden],[aria-hidden='true']").forEach((node) => node.remove());
    const links = [...publicDocument.querySelectorAll("a[href]")].filter((link) => new URL(link.href).pathname === TARGET_PATH && normalize(link.textContent) === ANCHOR);
    if (!publicPage.response.ok || links.length !== 1 || !normalize(publicDocument.body.textContent).includes(normalize(NEW_SENTENCE))) throw new Error(`Revised public sentence verification failed; visible anchors=${links.length}`);
    revision.rendered_verified = true;
    revision.completed_at = new Date().toISOString();
    report.current_sentence = NEW_SENTENCE;
    report.current_target_evidence = TARGET_FACT;
    report.current_revision_deployment_id = revision.deployment_id;
    report.completed_at = revision.completed_at;
    await save(report);
    console.log(`topic-kuwait:1 revised with destination data and publicly verified; deployment ${revision.deployment_id}`);
  } catch (error) {
    revision.error = error instanceof Error ? error.message : String(error);
    if (revision.deployment_id) {
      const rollback = await request(`${base}/internal/deployments/${revision.deployment_id}/rollback/`, { method: "POST", headers });
      revision.rollback_status = rollback.response.ok ? "rolled_back" : `failed_http_${rollback.response.status}`;
    }
    revision.completed_at = new Date().toISOString();
    await save(report);
    throw error;
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
