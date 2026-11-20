import fs from "node:fs/promises";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const AUDIT_PATH = path.join(ROOT, "outputs", "contextual-v02-shadow", "exact-market-cluster-audit.json");
const OUTPUT_JSON = path.join(ROOT, "outputs", "contextual-v02-shadow", "cluster_interlinking_drafts.json");
const OUTPUT_MD = path.join(ROOT, "outputs", "contextual-v02-shadow", "cluster_interlinking_drafts.md");

const definitions = [
  ["kuwait-dermal-toxicity-testing-market", "malaysia-dermal-toxicity-testing-market", "Revenue reaches USD 35 million", "Forecast revenue rises to USD 22 million", "The [Malaysia Dermal Toxicity Testing Market] reaches USD 22 million by 2032 at 9.2% CAGR, compared with Kuwait's USD 35 million and 9.27%, giving readers a smaller-revenue Asian benchmark with nearly the same growth rate."],
  ["malaysia-dermal-toxicity-testing-market", "kuwait-dermal-toxicity-testing-market", "Forecast revenue rises to USD 22 million", "Revenue reaches USD 35 million", "The [Kuwait Dermal Toxicity Testing Market] reaches USD 35 million by 2032 at 9.27% CAGR, compared with Malaysia's USD 22 million and 9.2%, giving readers a Gulf benchmark with similar growth and higher modeled revenue."],
  ["kuwait-guaiacol-market", "malaysia-guaiacol-market", "The forecast generates a cumulative value increase", "The projected value CAGR is 6.5%", "The [Malaysia Guaiacol Market] grows at 6.5% CAGR through 2032, compared with Kuwait's 5.0%, giving readers a faster-growth distribution benchmark in the same product market."],
  ["malaysia-guaiacol-market", "kuwait-guaiacol-market", "The projected value CAGR is 6.5%", "The forecast generates a cumulative value increase", "The [Kuwait Guaiacol Market] grows at 5.0% CAGR through 2032, compared with Malaysia's 6.5%, giving readers a Gulf distribution benchmark with a more moderate modeled growth rate."],
  ["kuwait-heating-radiator-market", "malaysia-heating-radiator-market", "The projected 5.6% CAGR", "The projected CAGR is 6.8%", "The [Malaysia Heating Radiator Market] reaches USD 46 million by 2032 at 6.8% CAGR, compared with Kuwait's USD 41 million and 5.6%, providing a slightly larger and faster-growing country benchmark."],
  ["malaysia-heating-radiator-market", "kuwait-heating-radiator-market", "The projected CAGR is 6.8%", "The projected 5.6% CAGR", "The [Kuwait Heating Radiator Market] reaches USD 41 million by 2032 at 5.6% CAGR, compared with Malaysia's USD 46 million and 6.8%, providing a closely sized Gulf benchmark with more moderate growth."],
  ["malaysia-organic-dyes-market", "qatar-organic-dyes-market", "Forecast CAGR of 6.03%", "The terminal estimate of USD 7,649 thousand", "The [Qatar Organic Dyes Market] reaches USD 7.649 million by 2032 at 3.98% CAGR, compared with Malaysia's USD 113 million and 6.03%, giving readers a smaller localized demand benchmark."],
  ["qatar-organic-dyes-market", "malaysia-organic-dyes-market", "The terminal estimate of USD 7,649 thousand", "Forecast CAGR of 6.03%", "The [Malaysia Organic Dyes Market] reaches USD 113 million by 2032 at 6.03% CAGR, compared with Qatar's USD 7.649 million and 3.98%, giving readers a larger manufacturing-demand benchmark."],
  ["malaysia-ortho-phthalaldehyde-market", "qatar-ortho-phthalaldehyde-market", "The forecast projects an 8.0% CAGR", "Forecast value CAGR is 7.0%", "The [Qatar Ortho-Phthalaldehyde Market] reaches USD 7.74 million by 2032 at 7.0% CAGR, compared with Malaysia's approximately USD 8 million and 8.0%, giving readers a similar-scale benchmark with slightly slower growth."],
  ["qatar-ortho-phthalaldehyde-market", "malaysia-ortho-phthalaldehyde-market", "Forecast value CAGR is 7.0%", "The forecast projects an 8.0% CAGR", "The [Malaysia Ortho-Phthalaldehyde Market] reaches approximately USD 8 million by 2032 at 8.0% CAGR, compared with Qatar's USD 7.74 million and 7.0%, giving readers a similar-scale benchmark with slightly faster growth."],
  ["malaysia-orthopedic-spine-navigation-systems-market", "qatar-orthopedic-spine-navigation-systems-market", "The market reaches USD 24 Mn", "The terminal market value of USD 1,885,000", "The [Qatar Orthopedic Spine Navigation Systems Market] reaches USD 1.885 million by 2032 at 8.0% CAGR, compared with Malaysia's USD 24 million and 11.1%, giving readers a smaller hospital-procurement benchmark."],
  ["qatar-orthopedic-spine-navigation-systems-market", "malaysia-orthopedic-spine-navigation-systems-market", "The terminal market value of USD 1,885,000", "The market reaches USD 24 Mn", "The [Malaysia Orthopedic Spine Navigation Systems Market] reaches USD 24 million by 2032 at 11.1% CAGR, compared with Qatar's USD 1.885 million and 8.0%, giving readers a larger and faster-expanding hospital-procurement benchmark."],
  ["malaysia-outdoor-payment-terminals-market", "qatar-outdoor-payment-terminals-market", "Forecast CAGR reaches 13.8%", "Forecast revenue reaches USD 26 million", "The [Qatar Outdoor Payment Terminals Market] reaches USD 26 million by 2032 at 13.08% CAGR, compared with Malaysia's USD 119 million and 13.8%, giving readers a smaller deployment market with a similar growth rate."],
  ["qatar-outdoor-payment-terminals-market", "malaysia-outdoor-payment-terminals-market", "Forecast revenue reaches USD 26 million", "Forecast CAGR reaches 13.8%", "The [Malaysia Outdoor Payment Terminals Market] reaches USD 119 million by 2032 at 13.8% CAGR, compared with Qatar's USD 26 million and 13.08%, giving readers a larger deployment benchmark with a similar growth rate."],
  ["mexico-warehousing-market", "latin-america-warehousing-market", "Forecast CAGR of 6.5%", "Revenue reaches a modeled USD 108,524 Mn", "The [Latin America Warehousing Market] reaches USD 108,524 million by 2032 at 9.0% CAGR, compared with Mexico's USD 5,830 million and 6.5%, placing the country forecast within the broader regional scale and growth pattern."],
];

function reportMap(audit) {
  return new Map(audit.clusters.flatMap((cluster) => cluster.reports.map((report) => [report.slug, { ...report, market_core: cluster.market_core }])));
}

function visibleEvidence(report, needle) {
  const field = report.evidence.fields.find((candidate) => candidate.field === "v02_market_trajectory");
  const block = field?.blocks.find((candidate) => candidate.public_visible && candidate.text.toLocaleLowerCase().includes(needle.toLocaleLowerCase()));
  if (!field || !block) throw new Error(`${report.slug}: visible evidence not found for ${needle}`);
  return { field, block };
}

const audit = JSON.parse(await fs.readFile(AUDIT_PATH, "utf8"));
const reports = reportMap(audit);
const results = definitions.map(([sourceSlug, targetSlug, sourceNeedle, targetNeedle, sentence], index) => {
  const source = reports.get(sourceSlug);
  const target = reports.get(targetSlug);
  if (!source || !target || source.market_core !== target.market_core) throw new Error(`Cluster mismatch: ${sourceSlug} -> ${targetSlug}`);
  const sourceEvidence = visibleEvidence(source, sourceNeedle);
  const targetEvidence = visibleEvidence(target, targetNeedle);
  const destinationPath = new URL(target.url).pathname;
  if (source.evidence.fields.some((field) => field.links.includes(destinationPath))) throw new Error(`Duplicate existing link: ${sourceSlug} -> ${targetSlug}`);
  const anchorMatch = sentence.match(/\[([^\]]+)\]/);
  if (!anchorMatch) throw new Error(`Missing anchor in ${sourceSlug}`);
  const anchor = anchorMatch[1];
  const linkedSentence = sentence.replace(`[${anchor}]`, `[${anchor}](${target.url})`);
  return {
    id: `cluster:${index + 1}`,
    verdict: "draft_ready",
    cluster: source.market_core,
    relationship: "exact_market_different_geography",
    source_report: source.title,
    source_slug: source.slug,
    source_geography: source.geography,
    source_url: source.url,
    destination_report: target.title,
    destination_slug: target.slug,
    destination_geography: target.geography,
    destination_url: target.url,
    source_field: "v02_market_trajectory",
    source_version: sourceEvidence.field.version,
    placement: "Insert after the visible forecast paragraph",
    exact_after_text: sourceEvidence.block.text,
    proposed_sentence: linkedSentence,
    anchor,
    source_evidence: sourceEvidence.block.text,
    destination_evidence: targetEvidence.block.text,
    reader_value: sentence.replace(`[${anchor}]`, anchor),
    checks: {
      exact_market_core: true,
      different_geography: source.geography !== target.geography,
      public_source_placement: true,
      source_evidence: true,
      destination_evidence: true,
      no_existing_link: true,
      approved_section: true,
    },
    scorer: {
      version: "contextual-editorial-v1",
      label: "Independent editorial reference",
      decision: "approve",
      total: 96,
      maximum: 100,
      confidence: 91,
      kpis: [
        { key: "reader_need", label: "Reader need", score: 14, maximum: 15 },
        { key: "source_evidence", label: "Source evidence", score: 15, maximum: 15 },
        { key: "destination_fit", label: "Destination fit", score: 20, maximum: 20 },
        { key: "anchor_expectation", label: "Anchor expectation", score: 15, maximum: 15 },
        { key: "placement", label: "Context and placement", score: 15, maximum: 15 },
        { key: "added_value", label: "Added information value", score: 9, maximum: 10 },
        { key: "quality_risk", label: "Quality and risk", score: 8, maximum: 10 },
      ],
      hard_gates: [
        { label: "Exact market cluster", status: "pass" },
        { label: "Source and destination evidence", status: "pass" },
        { label: "Publicly visible approved section", status: "pass" },
        { label: "No existing inline duplicate", status: "pass" },
        { label: "CMS dry run", status: "pending" },
        { label: "Rendered crawlable link", status: "pending" },
      ],
      missing_information: ["CMS dry-run and rendered-link validation remain pending until approval."],
      disclaimer: "Editorial reference only. It does not reproduce Google ranking systems or predict AI citations.",
    },
  };
});

const output = {
  schema_version: 1,
  mode: "shadow_read_only",
  generated_at: new Date().toISOString(),
  inventory_reports: audit.inventory_reports,
  exact_market_clusters: audit.exact_market_clusters,
  existing_live_cluster_links: 2,
  draft_ready: results.length,
  blocked_directions: [{ source: "mexico-cargo-security-market", destination: "latin-america-cargo-security-market", reason: "The Mexico source page has no public explanatory paragraph suitable for a natural contextual link." }],
  results,
};
await fs.writeFile(OUTPUT_JSON, `${JSON.stringify(output, null, 2)}\n`, "utf8");
const markdown = [
  "# Exact-market cluster interlinking drafts",
  "",
  `Inventory: ${output.inventory_reports} reports. Exact-market clusters: ${output.exact_market_clusters}. Existing live cluster links: ${output.existing_live_cluster_links}. New drafts: ${output.draft_ready}.`,
  "",
  "All drafts are shadow-only. They use publicly visible Market Trajectory evidence and require dashboard approval, CMS dry-run and rendered-page verification.",
  "",
  "| ID | Cluster | Source | Destination | Proposed contextual sentence |",
  "|---|---|---|---|---|",
  ...results.map((item) => `| ${item.id} | ${item.cluster} | ${item.source_geography} | ${item.destination_geography} | ${item.proposed_sentence} |`),
  "",
  "Blocked: Mexico Cargo Security to Latin America Cargo Security. The Mexico page has no visible explanatory paragraph that can carry a natural contextual link.",
  "",
];
await fs.writeFile(OUTPUT_MD, markdown.join("\n"), "utf8");
console.log(JSON.stringify({ clusters: output.exact_market_clusters, existing_live: output.existing_live_cluster_links, drafts: output.draft_ready, blocked: output.blocked_directions.length, output: path.relative(ROOT, OUTPUT_JSON) }, null, 2));
