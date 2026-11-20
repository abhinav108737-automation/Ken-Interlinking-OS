import fs from "node:fs/promises";
import path from "node:path";
import { JSDOM } from "jsdom";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const DRAFTS_PATH = path.join(ROOT, "outputs", "contextual-v02-shadow", "kuwait_topic_cluster_drafts.json");
const DECISIONS_PATH = path.join(ROOT, "data", "v02-contextual-review", "decision-state.json");
const REPORT_PATH = path.join(ROOT, "outputs", "contextual-v02-live", "topic-kuwait-1-live-2026-11-20.json");
const ID = "topic-kuwait:1";
const EXECUTE = process.argv.includes("--execute");

function parseEnv(text) {
  return Object.fromEntries(text.split(/\r?\n/).filter((line) => line.trim() && !line.trim().startsWith("#") && line.includes("=")).map((line) => {
    const split = line.indexOf("=");
    return [line.slice(0, split).trim(), line.slice(split + 1).trim().replace(/^['"]|['"]$/g, "")];
  }));
}

const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const countInsensitive = (text, needle) => text.toLocaleLowerCase().split(needle.toLocaleLowerCase()).length - 1;

async function request(url, options = {}) {
  const response = await fetch(url, { ...options, redirect: "manual", signal: AbortSignal.timeout(45_000) });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { non_json: true }; }
  return { response, data, text };
}

async function save(report) {
  await fs.mkdir(path.dirname(REPORT_PATH), { recursive: true });
  await fs.writeFile(REPORT_PATH, `${JSON.stringify(report, null, 2)}\n`, "utf8");
}

async function main() {
  const env = parseEnv(await fs.readFile(path.join(ROOT, ".env"), "utf8"));
  const base = String(env.KEN_CMS_BASE_URL || "").replace(/\/$/, "");
  if (!["thekenconsulting.com", "www.thekenconsulting.com"].includes(new URL(base).hostname)) throw new Error("Refusing unexpected CMS host");
  if (!env.KEN_CMS_WRITE_TOKEN) throw new Error("KEN_CMS_WRITE_TOKEN is missing");
  if (EXECUTE && String(process.env.KEN_CMS_WRITES_ENABLED ?? env.KEN_CMS_WRITES_ENABLED).toLowerCase() !== "true") throw new Error("KEN_CMS_WRITES_ENABLED must be true for execution");

  const drafts = JSON.parse(await fs.readFile(DRAFTS_PATH, "utf8"));
  const decisions = JSON.parse(await fs.readFile(DECISIONS_PATH, "utf8"));
  const draft = drafts.results.find((item) => item.id === ID);
  if (!draft || draft.verdict !== "draft_ready") throw new Error(`${ID} is not an eligible draft`);
  if (decisions.decisions?.[ID]?.decision !== "approve") throw new Error(`${ID} has no recorded approval`);

  const sourceSlug = draft.source_slug;
  const pageId = `${sourceSlug}:${draft.source_field}`;
  const pageUrl = `${base}/internal/pages/${pageId}/`;
  const href = new URL(draft.destination_url).pathname;
  const headers = { Accept: "application/json", Authorization: `Bearer ${env.KEN_CMS_WRITE_TOKEN}`, "Content-Type": "application/json" };
  const pageResult = await request(pageUrl, { headers });
  if (!pageResult.response.ok) throw new Error(`CMS read failed with HTTP ${pageResult.response.status}`);
  const page = pageResult.data;
  if (page.protected) throw new Error("Source field is protected");
  if (page.version !== draft.source_version) throw new Error("Approved source version is stale; re-review required");

  const document = new JSDOM(String(page.body_html || "")).window.document;
  const matches = [...document.querySelectorAll("p")].filter((node) => normalize(node.textContent) === normalize(draft.exact_after_text));
  if (matches.length !== 1) throw new Error(`Expected one exact placement paragraph; found ${matches.length}`);
  if ([...document.querySelectorAll("a[href]")].some((link) => new URL(link.href, draft.source_url).pathname === href)) throw new Error("Destination is already linked in this field");

  const markdown = `[${draft.anchor}](${draft.destination_url})`;
  if (draft.proposed_sentence.split(markdown).length - 1 !== 1) throw new Error("Approved sentence contains an invalid link placeholder");
  const plainSentence = draft.proposed_sentence.replace(markdown, draft.anchor);
  const bridgeDocument = new JSDOM("<p></p>").window.document;
  const bridge = bridgeDocument.querySelector("p");
  const [beforeAnchor, afterAnchor] = plainSentence.split(draft.anchor);
  bridge.append(beforeAnchor);
  const anchorNode = bridgeDocument.createElement("a");
  anchorNode.href = `https://thekenconsulting.com${href}`;
  anchorNode.textContent = draft.anchor;
  bridge.append(anchorNode, afterAnchor);
  const insertionMarker = "Renewal value will depend on how much of the recommended order volume planners trust enough to execute.";
  if (String(page.body_html || "").split(insertionMarker).length - 1 !== 1) throw new Error("The approved insertion marker is no longer unique in CMS content");
  const contentChange = { op: "insert_after", after_text: insertionMarker, sentence: bridge.innerHTML };

  const contentValidation = await request(`${pageUrl}content-changes/validate/`, {
    method: "POST", headers, body: JSON.stringify({ expected_version: page.version, change: contentChange }),
  });
  if (!contentValidation.response.ok || contentValidation.data?.valid !== true) throw new Error(`Content validation failed with HTTP ${contentValidation.response.status}: ${JSON.stringify(contentValidation.data)}`);

  const report = {
    schema_version: 1,
    id: ID,
    mode: EXECUTE ? "live_test_canary" : "dry_run",
    environment_note: "The staging host declares noindex,nofollow; this canary tests presentation only and is not an authority deployment.",
    source_url: draft.source_url,
    destination_url: draft.destination_url,
    page_id: pageId,
    source_field: draft.source_field,
    anchor: draft.anchor,
    proposed_sentence: draft.proposed_sentence,
    insertion_marker: insertionMarker,
    expected_version: page.version,
    content_validation: "valid",
    link_validation: "included_in_atomic_content_validation",
    deployment_ids: [],
    cms_verified: false,
    rendered_verified: false,
    rollback_status: null,
    started_at: new Date().toISOString(),
  };
  await save(report);
  if (!EXECUTE) {
    report.completed_at = new Date().toISOString();
    await save(report);
    console.log(`${ID}: content dry-run valid; no CMS writes performed`);
    return;
  }

  const operationBase = "ken-topic-kuwait-1-20261120-v1";
  try {
    const contentApply = await request(`${pageUrl}content-changes/`, {
      method: "POST", headers, body: JSON.stringify({ operation_id: `${operationBase}-content`, expected_version: page.version, change: contentChange }),
    });
    if (!contentApply.response.ok || contentApply.data?.deployment_id == null) throw new Error(`Content apply failed or was ambiguous with HTTP ${contentApply.response.status}`);
    report.deployment_ids.push(String(contentApply.data.deployment_id));
    report.content_deployment_id = String(contentApply.data.deployment_id);
    await save(report);

    const afterLink = await request(pageUrl, { headers });
    const afterBody = String(afterLink.data?.body_html || "");
    const cmsAnchor = new RegExp(`<a[^>]+href=["'][^"']*${escapeRegExp(href)}["'][^>]*>\\s*${escapeRegExp(draft.anchor)}\\s*<\\/a>`, "i");
    const afterDocument = new JSDOM(afterBody).window.document;
    if (!afterLink.response.ok || !cmsAnchor.test(afterBody) || countInsensitive(normalize(afterDocument.body.textContent), plainSentence) !== 1) throw new Error("Atomic sentence and anchor were not verified in CMS content");
    report.content_verified = true;
    report.cms_verified = true;
    report.final_version = afterLink.data.version;
    await save(report);

    for (let attempt = 0; attempt < 6; attempt += 1) {
      const revalidation = await request("https://www.thekenconsulting.com/api/revalidate", {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, body: JSON.stringify({ slug: sourceSlug, type: "v0.2" }),
      });
      if (!revalidation.response.ok) throw new Error(`Public revalidation failed with HTTP ${revalidation.response.status}`);
    }

    const publicResult = await request(draft.source_url, { headers: { Accept: "text/html", "Cache-Control": "no-cache" } });
    if (!publicResult.response.ok) throw new Error(`Public page returned HTTP ${publicResult.response.status}`);
    const publicDocument = new JSDOM(publicResult.text, { url: draft.source_url }).window.document;
    publicDocument.querySelectorAll("script,style,noscript,template,[hidden],[aria-hidden='true']").forEach((node) => node.remove());
    const publicLinks = [...publicDocument.querySelectorAll("a[href]")].filter((link) => new URL(link.href, draft.source_url).pathname === href && normalize(link.textContent) === draft.anchor);
    if (publicLinks.length !== 1) throw new Error(`Expected one visible rendered anchor; found ${publicLinks.length}`);
    if (!normalize(publicDocument.body.textContent).includes(normalize(plainSentence))) throw new Error("Inserted sentence is not visibly rendered on the public page");
    report.rendered_verified = true;
    report.public_anchor_href = publicLinks[0].href;
    report.completed_at = new Date().toISOString();
    await save(report);
    console.log(`${ID}: live and publicly verified; deployments ${report.deployment_ids.join(", ")}`);
  } catch (error) {
    report.error = error instanceof Error ? error.message : String(error);
    const rollbacks = [];
    for (const deploymentId of [...report.deployment_ids].reverse()) {
      const rollback = await request(`${base}/internal/deployments/${deploymentId}/rollback/`, { method: "POST", headers });
      rollbacks.push({ deployment_id: deploymentId, status: rollback.response.ok ? "rolled_back" : `failed_http_${rollback.response.status}` });
    }
    report.rollback_status = rollbacks;
    report.completed_at = new Date().toISOString();
    await save(report);
    throw error;
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
