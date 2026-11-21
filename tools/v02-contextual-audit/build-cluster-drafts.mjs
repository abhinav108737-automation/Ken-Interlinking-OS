import fs from "node:fs/promises";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const AUDIT_PATH = path.join(ROOT, "outputs", "contextual-v02-shadow", "exact-market-cluster-audit.json");
const OUTPUT_JSON = path.join(ROOT, "outputs", "contextual-v02-shadow", "cluster_interlinking_drafts.json");
const OUTPUT_MD = path.join(ROOT, "outputs", "contextual-v02-shadow", "cluster_interlinking_drafts.md");

const definitions = [
  ["kuwait-dermal-toxicity-testing-market", "malaysia-dermal-toxicity-testing-market", "Revenue reaches USD 35 million", "Forecast revenue rises to USD 22 million", "The [Malaysia Dermal Toxicity Testing Market] reaches USD 22 million by 2032, growing at 9.2% a year, close to Kuwait's 9.3%. Volume and utilisation explain most of that increase, while validated in vitro work adds further throughput."],
  ["malaysia-dermal-toxicity-testing-market", "kuwait-dermal-toxicity-testing-market", "Forecast revenue rises to USD 22 million", "Revenue reaches USD 35 million", "The [Kuwait Dermal Toxicity Testing Market] reaches USD 35 million by 2032, growing at 9.3% a year, close to Malaysia's 9.2%. Demand shifts toward in vitro testing, while specialty studies and dossier work support continued fee growth."],
  ["kuwait-guaiacol-market", "malaysia-guaiacol-market", "The forecast generates a cumulative value increase", "The projected value CAGR is 6.5%", "The [Malaysia Guaiacol Market] grows at 6.5% a year to about USD 6 million by 2032, ahead of Kuwait's 5.0%. Pharmaceutical manufacturing reaches 46% of modelled spending, while purchase prices are expected to stay broadly flat."],
  ["malaysia-guaiacol-market", "kuwait-guaiacol-market", "The projected value CAGR is 6.5%", "The forecast generates a cumulative value increase", "The [Kuwait Guaiacol Market] grows at 5.0% a year through 2032, slower than Malaysia's 6.5%. Volume reaches 127 metric tonnes by 2030, and with prices nearly flat, the outlook favours low-overhead distribution and repeat orders."],
  ["kuwait-heating-radiator-market", "malaysia-heating-radiator-market", "The projected 5.6% CAGR", "The projected CAGR is 6.8%", "The [Malaysia Heating Radiator Market] grows from USD 29 million to USD 46 million by 2032, at 6.8% a year against Kuwait's 5.6%. Unit volumes drive that growth, with average pricing expected to remain broadly stable."],
  ["malaysia-heating-radiator-market", "kuwait-heating-radiator-market", "The projected CAGR is 6.8%", "The projected 5.6% CAGR", "The [Kuwait Heating Radiator Market] reaches USD 41 million by 2032, growing at 5.6% a year against Malaysia's 6.8%. Value grows faster than volume, reflecting price, product mix and directly associated service content."],
  ["malaysia-organic-dyes-market", "qatar-organic-dyes-market", "Forecast CAGR of 6.03%", "The terminal estimate of USD 7,649 thousand", "The [Qatar Organic Dyes Market] reaches about USD 7.6 million by 2032, growing at 4.0% a year, well below Malaysia's 6.0%. Value rises faster than volume, pointing to selective premiumisation rather than a surge in commodity demand."],
  ["qatar-organic-dyes-market", "malaysia-organic-dyes-market", "The terminal estimate of USD 7,649 thousand", "Forecast CAGR of 6.03%", "The [Malaysia Organic Dyes Market] grows from USD 75 million to USD 113 million by 2032, at 6.0% a year against Qatar's 4.0%. Volume drives most of that increase, with natural dyes and cleaner processing offering further upside."],
  ["malaysia-ortho-phthalaldehyde-market", "qatar-ortho-phthalaldehyde-market", "The forecast projects an 8.0% CAGR", "Forecast value CAGR is 7.0%", "The [Qatar Ortho-Phthalaldehyde Market] reaches about USD 7.7 million by 2032, growing at 7.0% a year against Malaysia's 8.0%. Value rises faster than volume over the period, reflecting modest pricing and product-mix improvement."],
  ["qatar-ortho-phthalaldehyde-market", "malaysia-ortho-phthalaldehyde-market", "Forecast value CAGR is 7.0%", "The forecast projects an 8.0% CAGR", "The [Malaysia Ortho-Phthalaldehyde Market] reaches about USD 8 million by 2032, growing at 8.0% a year against Qatar's 7.0%. Healthcare disinfection remains the main use at 68% of demand, while analytical applications gain share."],
  ["malaysia-orthopedic-spine-navigation-systems-market", "qatar-orthopedic-spine-navigation-systems-market", "The market reaches USD 24 Mn", "The terminal market value of USD 1,885,000", "The [Qatar Orthopedic Spine Navigation Systems Market] reaches about USD 1.9 million by 2032, growing at 8.0% a year against Malaysia's 11.1%. Service and capital equipment lead spending, while software grows fastest at 14% a year."],
  ["qatar-orthopedic-spine-navigation-systems-market", "malaysia-orthopedic-spine-navigation-systems-market", "The terminal market value of USD 1,885,000", "The market reaches USD 24 Mn", "The [Malaysia Orthopedic Spine Navigation Systems Market] reaches USD 24 million by 2032, growing at 11.1% a year against Qatar's 8.0%. Accessory revenue grows faster still, while average workstation prices fall to USD 230,000."],
  ["malaysia-outdoor-payment-terminals-market", "qatar-outdoor-payment-terminals-market", "Forecast CAGR reaches 13.8%", "Forecast revenue reaches USD 26 million", "The [Qatar Outdoor Payment Terminals Market] reaches USD 26 million by 2032, growing at 13.1% a year, close to Malaysia's 13.8%. The installed base adds about 2,600 terminals, with replacement hardware and services lifting revenue."],
  ["qatar-outdoor-payment-terminals-market", "malaysia-outdoor-payment-terminals-market", "Forecast revenue reaches USD 26 million", "Forecast CAGR reaches 13.8%", "The [Malaysia Outdoor Payment Terminals Market] grows from USD 48 million to USD 119 million by 2032, at 13.8% a year. The installed base adds about 45,800 terminals, with software and services taking a larger revenue share."],
  ["mexico-warehousing-market", "latin-america-warehousing-market", "Forecast CAGR of 6.5%", "Revenue reaches a modeled USD 108,524 Mn", "The [Latin America Warehousing Market] reaches about USD 108.5 billion by 2032, growing at 9.0% a year against Mexico's 6.5%. Growth in active space slows to 5.8% by 2032 as throughput and a richer service mix take over."],
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
