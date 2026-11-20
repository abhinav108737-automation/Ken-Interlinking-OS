import fs from "node:fs/promises";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const PROPOSALS = path.join(ROOT, "data", "v02-contextual-review", "proposals.json");
const OUTPUT_DIR = path.join(ROOT, "outputs", "contextual-v02-shadow");
const OUTPUT_JSON = path.join(OUTPUT_DIR, "v02_contextual_audit_results.json");
const OUTPUT_MD = path.join(OUTPUT_DIR, "v02_contextual_audit_results.md");
const ALLOWED_SOURCE_FIELDS = new Set([
  "v02_growth_drivers",
  "v02_market_trajectory",
  "v02_market_segmentation",
  "v02_regional_analysis",
]);
const BLOCKED_FIELDS = new Set(["v02_market_overview", "v02_related_reports"]);
const PUSHY = /\b(click here|read more|learn more|must[- ]read|do not miss|don't miss|act now|exclusive|shocking|secret|unlock|discover now)\b|!{2,}/i;
const GENERIC = new Set(["market", "markets", "platform", "platforms", "system", "systems", "report", "reports", "kuwait", "cloud", "based", "size", "share", "the", "and", "for", "with", "from", "into"]);

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

function decode(text) {
  return String(text ?? "")
    .replace(/&nbsp;/gi, " ").replace(/&amp;/gi, "&").replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'").replace(/&lt;/gi, "<").replace(/&gt;/gi, ">");
}

function plain(html) {
  return decode(String(html ?? "").replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " "))
    .replace(/\s+/g, " ").trim();
}

function words(text) {
  return String(text ?? "").toLowerCase().match(/[a-z0-9]+/g) ?? [];
}

function root(word) {
  if (word.length > 5 && word.endsWith("ies")) return `${word.slice(0, -3)}y`;
  if (word.length > 5 && word.endsWith("ing")) return word.slice(0, -3);
  if (word.length > 4 && word.endsWith("s")) return word.slice(0, -1);
  return word;
}

function usefulWords(text) {
  return new Set(words(text).map(root).filter((word) => word.length > 2 && !GENERIC.has(word)));
}

function slugFrom(url) {
  return new URL(url).pathname.split("/").filter(Boolean).at(-1);
}

function fieldFrom(placement) {
  return String(placement).split(",", 1)[0].trim();
}

function anchorFrom(proposal) {
  const matches = [...String(proposal.proposed_wording).matchAll(/[“"]([^”"]+)[”"]/g)]
    .map((match) => match[1].trim()).filter((text) => words(text).length >= 2);
  return matches[0] ?? "";
}

function excerpt(text, needle, radius = 230) {
  const source = String(text ?? "");
  const index = source.toLowerCase().indexOf(String(needle).toLowerCase());
  if (index < 0) return "";
  const start = Math.max(0, index - radius);
  const end = Math.min(source.length, index + String(needle).length + radius);
  return source.slice(start, end).replace(/^\S*\s/, start ? "…" : "").replace(/\s\S*$/, end < source.length ? "…" : "");
}

function overlap(left, right) {
  const a = usefulWords(left);
  const b = usefulWords(right);
  if (!a.size || !b.size) return 0;
  return [...a].filter((word) => b.has(word)).length / Math.min(a.size, b.size);
}

function sharedWords(left, right) {
  const a = usefulWords(left);
  const b = usefulWords(right);
  return [...a].filter((word) => b.has(word));
}

function visibleProse(html) {
  const elements = [];
  for (const match of String(html ?? "").matchAll(/<(p|li)\b[^>]*>([\s\S]*?)<\/\1>/gi)) {
    const text = plain(match[2]);
    if (words(text).length >= 10) elements.push({ tag: match[1].toLowerCase(), html: match[0], text });
  }
  return elements;
}

function destinationPath(url) {
  return new URL(url).pathname.replace(/\/$/, "");
}

function linkedAnchor(element, destinationUrl, preferredAnchor) {
  const destination = destinationPath(destinationUrl);
  const links = [...element.html.matchAll(/<a\b[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi)];
  return links.map((match) => ({ href: match[1].replace(/\/$/, ""), anchor: plain(match[2]) }))
    .find((link) => link.href === destination || destination.endsWith(link.href) || link.href.endsWith(destination))
    ?? (element.text.toLowerCase().includes(preferredAnchor.toLowerCase()) ? { href: null, anchor: preferredAnchor } : null);
}

function sourceEvidenceFor(fieldHtml, proposal, preferredAnchor) {
  const destination = destinationPath(proposal.destination_url);
  const candidates = visibleProse(fieldHtml).map((element) => {
    const link = linkedAnchor(element, proposal.destination_url, preferredAnchor);
    const contains = element.text.toLowerCase().includes(preferredAnchor.toLowerCase());
    const correctLink = link?.href === destination || destination.endsWith(link?.href ?? "") || (link?.href ?? "").endsWith(destination);
    return { ...element, contains, correctLink, anchor: link?.anchor || preferredAnchor };
  }).filter((item) => item.contains);
  candidates.sort((a, b) => Number(b.correctLink) - Number(a.correctLink) || words(b.text).length - words(a.text).length);
  return candidates[0] ?? null;
}

function bestTargetEvidence(report, anchor, destination, sourceEvidence) {
  const desired = `${anchor} ${destination} ${sourceEvidence}`;
  const candidates = report.fields.filter((field) => ALLOWED_SOURCE_FIELDS.has(field.field))
    .flatMap((field) => visibleProse(field.body_html)
      .filter((element) => !/comprehensive analysis of factors shaping/i.test(element.text))
      .map((element) => ({
        field: field.field,
        text: element.text,
        score: overlap(element.text, desired),
        shared: sharedWords(element.text, desired),
      })));
  candidates.sort((a, b) => b.score - a.score || a.field.localeCompare(b.field));
  return candidates[0] ?? null;
}

async function report(base, token, slug) {
  const response = await fetch(`${base}/internal/reports/${slug}/`, {
    headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new Error(`CMS read failed for ${slug} (HTTP ${response.status}).`);
  const data = await response.json();
  if (!Array.isArray(data.fields)) throw new Error(`CMS report ${slug} has no field collection.`);
  return data;
}

function review(proposal, source, target) {
  const sourceField = fieldFrom(proposal.placement);
  const proposedAnchor = anchorFrom(proposal);
  const field = source.fields.find((item) => item.field === sourceField);
  const sourceEvidence = sourceEvidenceFor(field?.body_html, proposal, proposedAnchor);
  const anchor = sourceEvidence?.anchor || proposedAnchor;
  const targetEvidence = bestTargetEvidence(target, anchor, proposal.destination_report, sourceEvidence?.text ?? "");
  const titleOverlap = overlap(anchor, proposal.destination_report);
  const contextWordCount = words(sourceEvidence?.text).length - words(anchor).length;
  const checks = {
    policy_eligible: proposal.policy_eligible === true,
    allowed_section: ALLOWED_SOURCE_FIELDS.has(sourceField),
    visible_explanatory_prose: Boolean(sourceEvidence && ["p", "li"].includes(sourceEvidence.tag) && contextWordCount >= 8),
    correct_existing_destination: Boolean(sourceEvidence?.correctLink),
    destination_supported: Boolean(targetEvidence && targetEvidence.score >= 0.15 && targetEvidence.shared.length >= 2),
    descriptive_anchor: words(anchor).length >= 2 && titleOverlap >= 0.4,
    neutral_tone: !PUSHY.test(`${proposal.proposed_wording} ${proposal.reader_value}`),
    no_new_claim: /^(Link existing|Prefer)/i.test(proposal.proposed_wording),
  };
  const hardPass = Object.values(checks).every(Boolean);
  const score = [checks.allowed_section, checks.descriptive_anchor, checks.visible_explanatory_prose,
    checks.destination_supported, checks.neutral_tone].reduce((sum, passed, index) => sum + (passed ? [25, 25, 20, 20, 10][index] : 0), 0);
  const failures = Object.entries(checks).filter(([, passed]) => !passed).map(([name]) => name);
  return {
    proposal_id: proposal.id,
    current_status: proposal.current_status,
    source_report: proposal.source_report,
    destination_report: proposal.destination_report,
    source_url: proposal.source_url,
    destination_url: proposal.destination_url,
    source_field: sourceField,
    expected_source_version: field?.version ?? null,
    anchor,
    checks,
    score,
    threshold: 80,
    verdict: hardPass && score >= 80 ? "draft_ready" : "refuse_to_draft",
    failures,
    evidence: {
      source_type: sourceEvidence?.tag ?? null,
      source: sourceEvidence?.text ?? "",
      target_field: targetEvidence?.field ?? null,
      target: targetEvidence?.text.slice(0, 520) ?? "",
      target_shared_terms: targetEvidence?.shared ?? [],
    },
    draft: hardPass && score >= 80 ? {
      action: "keep_existing_contextual_link",
      anchor,
      exact_find: anchor,
      before_copy: sourceEvidence?.text ?? "",
      after_copy: sourceEvidence?.text ?? "",
      wording_change: null,
      cms_validation: "not_run_read_only",
      explanation: "The existing visible sentence supplies the context. Adding another sentence would be unnecessary and could make the link feel pushy.",
    } : null,
  };
}

const env = parseEnv(await fs.readFile(path.join(ROOT, ".env"), "utf8"));
const base = String(env.KEN_CMS_BASE_URL || "").replace(/\/$/, "");
const host = new URL(base).hostname;
if (!["www.thekenconsulting.com", "thekenconsulting.com"].includes(host)) throw new Error("Unexpected CMS host.");
if (!env.KEN_CMS_READ_TOKEN) throw new Error("CMS read token is missing.");
const dataset = JSON.parse(await fs.readFile(PROPOSALS, "utf8"));
const eligible = dataset.proposals.filter((proposal) => proposal.policy_eligible === true);
const cache = new Map();
async function cached(slug) {
  if (!cache.has(slug)) cache.set(slug, report(base, env.KEN_CMS_READ_TOKEN, slug));
  return cache.get(slug);
}
const results = [];
for (const proposal of eligible) {
  const [source, target] = await Promise.all([cached(slugFrom(proposal.source_url)), cached(slugFrom(proposal.destination_url))]);
  results.push(review(proposal, source, target));
}
const audit = {
  schema_version: 1,
  dataset: dataset.dataset,
  editorial_policy: dataset.editorial_policy,
  generated_at: new Date().toISOString(),
  mode: "shadow_read_only",
  summary: {
    audited: results.length,
    draft_ready: results.filter((item) => item.verdict === "draft_ready").length,
    refused: results.filter((item) => item.verdict === "refuse_to_draft").length,
  },
  results,
};
const markdown = [
  "# v0.2 contextual reviewer and drafter audit",
  "",
  `Generated: ${audit.generated_at}`,
  "",
  `Audited ${audit.summary.audited}; draft-ready ${audit.summary.draft_ready}; refused ${audit.summary.refused}.`,
  "",
  "No CMS write was performed. A draft-ready result keeps an existing contextual phrase; this run does not invent or insert new factual copy.",
  "",
  ...results.flatMap((item) => [
    `## ${item.proposal_id}: ${item.source_report} → ${item.destination_report}`,
    "",
    `- Verdict: **${item.verdict}** (${item.score}/${item.threshold})`,
    `- Placement: \`${item.source_field}\``,
    `- Anchor: **${item.anchor || "none"}**`,
    `- Failed checks: ${item.failures.length ? item.failures.join(", ") : "none"}`,
    `- Draft: ${item.draft ? "keep the existing contextual link; add no new sentence" : "none"}`,
    `- Source evidence: ${item.evidence.source || "not found"}`,
    `- Target evidence (${item.evidence.target_field || "none"}): ${item.evidence.target || "not found"}`,
    "",
  ]),
].join("\n");
await fs.mkdir(OUTPUT_DIR, { recursive: true });
await fs.writeFile(OUTPUT_JSON, JSON.stringify(audit, null, 2) + "\n", "utf8");
await fs.writeFile(OUTPUT_MD, markdown + "\n", "utf8");
console.log(JSON.stringify({ output_json: OUTPUT_JSON, output_markdown: OUTPUT_MD, ...audit.summary }, null, 2));
