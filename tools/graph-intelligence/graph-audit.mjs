import fs from "node:fs/promises";
import path from "node:path";
import { JSDOM } from "jsdom";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const OUTPUT = path.join(ROOT, "outputs", "graph-intelligence");
const normalizeUrl = (value) => { const url = new URL(value); url.hash = ""; return url.href.replace(/\/$/, ""); };

async function readJson(file) { return JSON.parse(await fs.readFile(file, "utf8")); }
async function writeJson(file, value) { await fs.mkdir(path.dirname(file), { recursive: true }); await fs.writeFile(file, `${JSON.stringify(value, null, 2)}\n`, "utf8"); }

function graphStats(nodes, edges) {
  const inbound = new Map(nodes.map((node) => [node.id, 0]));
  const outbound = new Map(nodes.map((node) => [node.id, 0]));
  for (const edge of edges) {
    outbound.set(edge.source, (outbound.get(edge.source) || 0) + 1);
    inbound.set(edge.target, (inbound.get(edge.target) || 0) + 1);
  }
  return nodes.map((node) => ({ ...node, inbound: inbound.get(node.id) || 0, outbound: outbound.get(node.id) || 0, orphan: (inbound.get(node.id) || 0) === 0 }));
}

async function buildKuwait() {
  const evidence = await readJson(path.join(ROOT, "outputs", "contextual-v02-shadow", "kuwait_topic_evidence.json"));
  const drafts = await readJson(path.join(ROOT, "outputs", "contextual-v02-shadow", "kuwait_topic_cluster_drafts.json"));
  const decisions = await readJson(path.join(ROOT, "data", "v02-contextual-review", "decision-state.json"));
  const firstLive = await readJson(path.join(ROOT, "outputs", "contextual-v02-live", "topic-kuwait-1-live-2026-11-20.json"));
  const batch = await readJson(path.join(ROOT, "outputs", "contextual-v02-live", "approved-topic-batch-2026-11-20.json"));
  const live = new Set([firstLive.rendered_verified ? firstLive.id : null, ...(batch.results || []).filter((item) => item.rendered_verified).map((item) => item.id)].filter(Boolean));
  const pathToId = new Map(evidence.reports.map((report) => [new URL(report.url).pathname, report.slug]));
  const groupBySlug = new Map();
  for (const group of drafts.topic_groups) for (const report of group.reports) groupBySlug.set(report.slug, group.name);
  const nodes = evidence.reports.map((report) => ({
    id: report.slug,
    title: report.title,
    url: report.url,
    group: groupBySlug.get(report.slug) || "Specialist",
    http_status: report.indexability?.http_status ?? report.public_status,
    noindex: report.indexability?.noindex ?? null,
    nofollow: report.indexability?.nofollow ?? null,
    canonical_url: report.indexability?.canonical_url ?? null,
  }));
  const edgeKey = (source, target, kind) => `${source}\u0000${target}\u0000${kind}`;
  const seen = new Set();
  const edges = [];
  for (const report of evidence.reports) {
    for (const link of report.public_links || []) {
      const target = pathToId.get(link.path);
      if (!target || target === report.slug) continue;
      const key = edgeKey(report.slug, target, "existing");
      if (seen.has(key)) continue;
      seen.add(key);
      edges.push({ source: report.slug, target, kind: "existing", anchor: link.text || "" });
    }
  }
  for (const draft of drafts.results) {
    const decision = decisions.decisions?.[draft.id]?.decision || "pending";
    const kind = live.has(draft.id) ? "live" : decision === "approve" ? "approved" : "proposed";
    const key = edgeKey(draft.source_slug, draft.destination_slug, kind);
    if (!seen.has(key)) {
      seen.add(key);
      edges.push({ source: draft.source_slug, target: draft.destination_slug, kind, anchor: draft.anchor, proposal_id: draft.id, score: draft.scorer.total });
    }
  }
  const snapshot = {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    scope: "Kuwait Cloud-Based topic reports",
    nodes: graphStats(nodes, edges.filter((edge) => ["existing", "live"].includes(edge.kind))),
    edges,
  };
  const file = path.join(OUTPUT, "kuwait-topic-graph-current.json");
  await writeJson(file, snapshot);
  console.log(JSON.stringify({ file: path.relative(ROOT, file), nodes: nodes.length, edges: edges.length, live_edges: edges.filter((edge) => edge.kind === "live").length }, null, 2));
}

function shortestPath(snapshot, source, target) {
  const adjacency = new Map(snapshot.nodes.map((node) => [node.id, []]));
  for (const edge of snapshot.edges.filter((item) => ["existing", "live"].includes(item.kind))) adjacency.get(edge.source)?.push(edge);
  const queue = [source];
  const previous = new Map([[source, null]]);
  while (queue.length) {
    const current = queue.shift();
    if (current === target) break;
    for (const edge of adjacency.get(current) || []) if (!previous.has(edge.target)) { previous.set(edge.target, { node: current, edge }); queue.push(edge.target); }
  }
  if (!previous.has(target)) return null;
  const result = [];
  for (let node = target; previous.get(node);) { const step = previous.get(node); result.unshift(step.edge); node = step.node; }
  return result;
}

async function diffSnapshots(beforeFile, afterFile) {
  const before = await readJson(path.resolve(beforeFile));
  const after = await readJson(path.resolve(afterFile));
  const keys = (snapshot) => new Set(snapshot.edges.map((edge) => `${edge.source}\u0000${edge.target}\u0000${edge.kind}`));
  const beforeNodes = new Set(before.nodes.map((node) => node.id));
  const afterNodes = new Set(after.nodes.map((node) => node.id));
  const beforeEdges = keys(before), afterEdges = keys(after);
  const result = {
    generated_at: new Date().toISOString(),
    new_nodes: [...afterNodes].filter((id) => !beforeNodes.has(id)),
    lost_nodes: [...beforeNodes].filter((id) => !afterNodes.has(id)),
    new_edges: [...afterEdges].filter((id) => !beforeEdges.has(id)),
    lost_edges: [...beforeEdges].filter((id) => !afterEdges.has(id)),
  };
  console.log(JSON.stringify(result, null, 2));
}

async function sitemapSeeds(baseUrl, outputFile) {
  const base = new URL(baseUrl);
  const siteHost = base.hostname.replace(/^www\./i, "");
  const sameSite = (value) => { const url = new URL(value, base); return url.protocol === base.protocol && url.hostname.replace(/^www\./i, "") === siteHost; };
  const robots = await fetch(new URL("/robots.txt", base), { signal: AbortSignal.timeout(30_000) });
  const robotsText = robots.ok ? await robots.text() : "";
  const declared = robotsText.split(/\r?\n/).map((line) => line.match(/^\s*sitemap\s*:\s*(.+)\s*$/i)?.[1]).filter(Boolean);
  const queue = [...new Set(declared.length ? declared : [new URL("/sitemap.xml", base).href])];
  const visited = new Set();
  const pages = new Set();
  while (queue.length && visited.size < 100) {
    const sitemap = queue.shift();
    if (!sameSite(sitemap) || visited.has(sitemap)) continue;
    visited.add(sitemap);
    const response = await fetch(sitemap, { headers: { Accept: "application/xml,text/xml" }, signal: AbortSignal.timeout(45_000) });
    if (!response.ok) continue;
    const document = new JSDOM(await response.text(), { contentType: "text/xml", url: sitemap }).window.document;
    const locations = [...document.querySelectorAll("loc")].map((node) => normalizeUrl(node.textContent));
    if (document.documentElement.localName === "sitemapindex") for (const location of locations) if (sameSite(location)) queue.push(location);
    else for (const location of locations) if (sameSite(location)) pages.add(location);
  }
  const result = { schema_version: 1, generated_at: new Date().toISOString(), base_url: base.href, sitemaps: [...visited], urls: [...pages].sort() };
  const file = path.resolve(outputFile || path.join(OUTPUT, "sitemap-seeds.json"));
  await writeJson(file, result);
  console.log(JSON.stringify({ file, sitemaps: visited.size, urls: pages.size }, null, 2));
}

const [command, ...args] = process.argv.slice(2);
if (command === "build-kuwait") await buildKuwait();
else if (command === "diff" && args.length === 2) await diffSnapshots(args[0], args[1]);
else if (command === "path" && args.length === 3) {
  const snapshot = await readJson(path.resolve(args[0]));
  console.log(JSON.stringify({ source: args[1], target: args[2], path: shortestPath(snapshot, args[1], args[2]) }, null, 2));
} else if (command === "sitemap" && args.length >= 1) await sitemapSeeds(args[0], args[1]);
else throw new Error("Usage: graph-audit.mjs build-kuwait | diff BEFORE AFTER | path SNAPSHOT SOURCE TARGET | sitemap BASE_URL [OUTPUT]");
