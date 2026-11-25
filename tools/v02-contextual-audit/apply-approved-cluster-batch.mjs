// Apply the dashboard-approved exact-market cluster drafts (`cluster:*`) to the Ken Consulting
// test CMS, under the same gates the approved topic batch used (ADR-0030):
//
//   read -> protected? -> version match -> exact placement paragraph -> destination not already
//   linked -> unique insertion marker -> validate (dry run) -> apply -> CMS verify -> public
//   revalidate -> rendered-DOM verify, and roll back on any failure after the write.
//
// Without `--execute` nothing is written: every draft is read and validated, and the report
// records the dry run. A draft whose source version moved since approval is REFUSED, never
// re-approved against the newer version: the approval was given for the text that was reviewed.
//
// `--drafts=` and `--report=` point it at another draft file in the same shape. `--unapproved`
// previews drafts that carry no approval yet, so they can be validated before review; it is
// refused together with `--execute`, so nothing unapproved can ever be written.
import fs from "node:fs/promises";
import path from "node:path";
import { JSDOM } from "jsdom";
import { copyRuleProblems } from "./copy-rules.mjs";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const option = (name) => process.argv.find((arg) => arg.startsWith(`--${name}=`))?.slice(name.length + 3);
const DRAFTS_PATH = path.resolve(ROOT, option("drafts") ?? path.join("outputs", "contextual-v02-shadow", "cluster_interlinking_drafts.json"));
const DECISIONS_PATH = path.join(ROOT, "data", "v02-contextual-review", "decision-state.json");
const REPORT_PATH = path.resolve(ROOT, option("report") ?? path.join("outputs", "contextual-v02-live", "approved-cluster-batch-2026-11-20.json"));
const EXECUTE = process.argv.includes("--execute");
const UNAPPROVED = process.argv.includes("--unapproved");
if (EXECUTE && UNAPPROVED) throw new Error("--unapproved is a preview: it can never be combined with --execute");
const ONLY = new Set(process.argv.slice(2).filter((arg) => !arg.startsWith("--")));

function parseEnv(text) {
  return Object.fromEntries(text.split(/\r?\n/).filter((line) => line.trim() && !line.trim().startsWith("#") && line.includes("=")).map((line) => {
    const split = line.indexOf("=");
    return [line.slice(0, split).trim(), line.slice(split + 1).trim().replace(/^['"]|['"]$/g, "")];
  }));
}

const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function insertionMarker(text, bodyHtml) {
  const candidates = normalize(text).split(/(?<=[.!?])\s+/).reverse();
  const document = new JSDOM(bodyHtml).window.document;
  const walker = document.createTreeWalker(document.body, 4);
  const textNodes = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) textNodes.push(node.nodeValue || "");
  for (const candidate of candidates) {
    const occurrences = textNodes.reduce((count, value) => count + value.split(candidate).length - 1, 0);
    if (candidate.split(/\s+/).length >= 7 && occurrences === 1) return candidate;
  }
  throw new Error("No unique final sentence is available as the insertion marker");
}

function linkedSentence(draft, targetPath) {
  const markdown = `[${draft.anchor}](${draft.destination_url})`;
  if (draft.proposed_sentence.split(markdown).length - 1 !== 1) throw new Error(`${draft.id}: invalid linked sentence placeholder`);
  const [before, after] = draft.proposed_sentence.split(markdown);
  const document = new JSDOM("<p></p>").window.document;
  const paragraph = document.querySelector("p");
  paragraph.append(before);
  const link = document.createElement("a");
  link.href = `https://thekenconsulting.com${targetPath}`;
  link.textContent = draft.anchor;
  paragraph.append(link, after);
  return { html: paragraph.innerHTML, plain: normalize(paragraph.textContent) };
}

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
  // A report that records live writes is the audit trail, and the dashboard reads live status from
  // it: no later run may overwrite it. Each run must name a fresh report with --report=.
  const existing = await fs.readFile(REPORT_PATH, "utf8").then(JSON.parse, () => null);
  if (String(existing?.mode ?? "").startsWith("live")) throw new Error(`${path.relative(ROOT, REPORT_PATH)} records live writes and is never overwritten; pass --report=<new file>`);
  const env = parseEnv(await fs.readFile(path.join(ROOT, ".env"), "utf8"));
  const base = String(env.KEN_CMS_BASE_URL || "").replace(/\/$/, "");
  if (!["thekenconsulting.com", "www.thekenconsulting.com"].includes(new URL(base).hostname)) throw new Error("Refusing unexpected CMS host");
  if (!env.KEN_CMS_WRITE_TOKEN) throw new Error("KEN_CMS_WRITE_TOKEN is missing");
  if (EXECUTE && String(process.env.KEN_CMS_WRITES_ENABLED ?? env.KEN_CMS_WRITES_ENABLED).toLowerCase() !== "true") throw new Error("KEN_CMS_WRITES_ENABLED must be true for execution");
  const headers = { Accept: "application/json", Authorization: `Bearer ${env.KEN_CMS_WRITE_TOKEN}`, "Content-Type": "application/json" };
  const drafts = JSON.parse(await fs.readFile(DRAFTS_PATH, "utf8"));
  const decisions = JSON.parse(await fs.readFile(DECISIONS_PATH, "utf8"));
  const approved = drafts.results.filter((draft) => (UNAPPROVED || decisions.decisions?.[draft.id]?.decision === "approve") && (ONLY.size === 0 || ONLY.has(draft.id)));
  if (approved.length === 0) throw new Error("No approved drafts were found");

  const prepared = [];
  const refused = [];
  for (const draft of approved) {
    const pageId = `${draft.source_slug}:${draft.source_field}`;
    const pageUrl = `${base}/internal/pages/${pageId}/`;
    const targetPath = new URL(draft.destination_url).pathname;
    try {
      // ADR-0031 and the owner's standing instruction (2026-11-25): no link is ever placed in Market
      // Overview or Related Reports, whatever a draft proposes. Refused before any request.
      if (/^v02_(market_overview|related_reports?)$/.test(draft.source_field)) throw new Error(`placement: ${draft.source_field} is a hard exclusion (ADR-0031)`);
      // The copy rule is checked before the page is even read: a draft that breaks it never
      // reaches the CMS, whichever builder produced it.
      const copyProblems = copyRuleProblems(linkedSentence(draft, targetPath).plain, { readerValue: draft.reader_value });
      if (copyProblems.length) throw new Error(`copy rule: ${copyProblems.join("; ")}`);
      const pageResult = await request(pageUrl, { headers });
      if (!pageResult.response.ok) throw new Error(`CMS read failed with HTTP ${pageResult.response.status}`);
      const page = pageResult.data;
      if (page.protected) throw new Error("source field is protected");
      if (page.version !== draft.source_version) throw new Error("approved source version is stale");
      const body = String(page.body_html || "");
      const sourceDocument = new JSDOM(body).window.document;
      const placementMatches = [...sourceDocument.querySelectorAll("p")].filter((node) => normalize(node.textContent) === normalize(draft.exact_after_text));
      if (placementMatches.length !== 1) throw new Error(`expected one exact placement paragraph; found ${placementMatches.length}`);
      if ([...sourceDocument.querySelectorAll("a[href]")].some((link) => new URL(link.href, draft.source_url).pathname === targetPath)) throw new Error("destination is already linked in the source field");
      const marker = insertionMarker(draft.exact_after_text, body);
      if (normalize(sourceDocument.body.textContent).split(marker).length - 1 !== 1) throw new Error("insertion marker is not unique in rendered CMS text");
      const sentence = linkedSentence(draft, targetPath);
      const change = { op: "insert_after", after_text: marker, sentence: sentence.html };
      const validation = await request(`${pageUrl}content-changes/validate/`, { method: "POST", headers, body: JSON.stringify({ expected_version: page.version, change }) });
      if (!validation.response.ok || validation.data?.valid !== true) throw new Error(`dry run failed with HTTP ${validation.response.status}: ${JSON.stringify(validation.data)}`);
      prepared.push({ draft, pageId, pageUrl, targetPath, marker, sentence, change, version: page.version });
      console.log(`${draft.id}: dry-run valid`);
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      refused.push({ id: draft.id, page_id: pageId, destination_url: draft.destination_url, reason });
      console.log(`${draft.id}: REFUSED -- ${reason}`);
    }
  }

  const report = {
    schema_version: 1,
    mode: EXECUTE ? "live_batch" : UNAPPROVED ? "dry_run_unapproved_preview" : "dry_run",
    drafts: path.relative(ROOT, DRAFTS_PATH),
    environment_note: "The staging host declares noindex,nofollow; this batch tests presentation and controlled publishing, not authority transfer.",
    approved_ids: approved.map((draft) => draft.id),
    validated_ids: prepared.map(({ draft }) => draft.id),
    refused,
    started_at: new Date().toISOString(),
    results: prepared.map(({ draft, pageId, targetPath, marker, sentence, version }) => ({
      id: draft.id,
      page_id: pageId,
      cluster: draft.cluster,
      source_url: draft.source_url,
      destination_url: draft.destination_url,
      target_path: targetPath,
      anchor: draft.anchor,
      proposed_sentence: sentence.plain,
      insertion_marker: marker,
      expected_version: version,
      score: draft.scorer?.total ?? null,
      validation: "valid",
      deployment_id: null,
      cms_verified: false,
      rendered_verified: false,
      rollback_status: null,
    })),
  };
  await save(report);
  if (!EXECUTE) {
    report.completed_at = new Date().toISOString();
    await save(report);
    console.log(`Validated ${prepared.length} of ${approved.length} drafts; no CMS writes performed`);
    return;
  }

  for (const item of prepared) {
    const record = report.results.find((result) => result.id === item.draft.id);
    const operationId = `ken-${item.draft.id.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}-20261120-v1`;
    try {
      record.operation_id = operationId;
      record.apply_started_at = new Date().toISOString();
      await save(report);
      const applied = await request(`${item.pageUrl}content-changes/`, { method: "POST", headers, body: JSON.stringify({ operation_id: operationId, expected_version: item.version, change: item.change }) });
      if (!applied.response.ok || applied.data?.deployment_id == null) throw new Error(`Apply failed or was ambiguous with HTTP ${applied.response.status}`);
      record.deployment_id = String(applied.data.deployment_id);
      await save(report);

      const after = await request(item.pageUrl, { headers });
      const afterBody = String(after.data?.body_html || "");
      const afterDocument = new JSDOM(afterBody).window.document;
      const anchorPattern = new RegExp(`<a[^>]+href=["'][^"']*${escapeRegExp(item.targetPath)}["'][^>]*>\\s*${escapeRegExp(item.draft.anchor)}\\s*<\\/a>`, "i");
      if (!after.response.ok || !anchorPattern.test(afterBody) || !normalize(afterDocument.body.textContent).includes(item.sentence.plain)) throw new Error("CMS sentence and anchor verification failed");
      record.cms_verified = true;
      record.final_version = after.data.version;
      await save(report);

      for (let attempt = 0; attempt < 6; attempt += 1) {
        const refreshed = await request("https://www.thekenconsulting.com/api/revalidate", { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, body: JSON.stringify({ slug: item.draft.source_slug, type: "v0.2" }) });
        if (!refreshed.response.ok) throw new Error(`Public revalidation failed with HTTP ${refreshed.response.status}`);
      }
      const publicPage = await request(item.draft.source_url, { headers: { Accept: "text/html", "Cache-Control": "no-cache" } });
      const publicDocument = new JSDOM(publicPage.text, { url: item.draft.source_url }).window.document;
      publicDocument.querySelectorAll("script,style,noscript,template,[hidden],[aria-hidden='true']").forEach((node) => node.remove());
      const publicLinks = [...publicDocument.querySelectorAll("a[href]")].filter((link) => new URL(link.href, item.draft.source_url).pathname === item.targetPath && normalize(link.textContent) === item.draft.anchor);
      if (!publicPage.response.ok || publicLinks.length !== 1 || !normalize(publicDocument.body.textContent).includes(item.sentence.plain)) throw new Error(`Public verification failed; visible anchors=${publicLinks.length}`);
      record.rendered_verified = true;
      record.public_anchor_href = publicLinks[0].href;
      record.completed_at = new Date().toISOString();
      await save(report);
      console.log(`${item.draft.id}: live and publicly verified; deployment ${record.deployment_id}`);
    } catch (error) {
      record.error = error instanceof Error ? error.message : String(error);
      if (record.deployment_id) {
        const rollback = await request(`${base}/internal/deployments/${record.deployment_id}/rollback/`, { method: "POST", headers });
        record.rollback_status = rollback.response.ok ? "rolled_back" : `failed_http_${rollback.response.status}`;
      }
      record.failed_at = new Date().toISOString();
      await save(report);
      console.log(`${item.draft.id}: FAILED -- ${record.error}; rollback ${record.rollback_status ?? "not needed"}`);
    }
  }
  report.completed_at = new Date().toISOString();
  await save(report);
  const live = report.results.filter((result) => result.rendered_verified).length;
  console.log(`\n${live} of ${prepared.length} links are live and publicly verified`);
}

await main();
