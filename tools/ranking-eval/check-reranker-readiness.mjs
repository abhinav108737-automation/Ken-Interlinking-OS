import fs from "node:fs/promises";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const input = JSON.parse(await fs.readFile(path.join(ROOT, "evals", "datasets", "kuwait-topic-real-judgments.json"), "utf8"));
const rows = input.judgments || [];
const counts = {
  decisions: rows.length,
  positive: rows.filter((item) => item.relevance === 1).length,
  negative: rows.filter((item) => item.relevance === 0).length,
  rendered_live: rows.filter((item) => item.rendered_live).length,
  measured_click_outcomes: rows.filter((item) => Number.isFinite(item.clicks)).length,
};
const thresholds = { decisions: 100, positive: 40, negative: 20, measured_click_outcomes: 50 };
const missing = Object.entries(thresholds).filter(([key, value]) => counts[key] < value).map(([key, value]) => ({ metric: key, actual: counts[key], required: value }));
const report = {
  schema_version: 1,
  generated_at: new Date().toISOString(),
  ready: missing.length === 0,
  counts,
  thresholds,
  missing,
  decision: missing.length ? "Do not train or promote a click reranker. Continue collecting real human labels and measured outcomes." : "Eligible for a shadow-only reranker experiment; production promotion still requires offline and canary evaluation.",
};
const file = path.join(ROOT, "outputs", "ranking-eval", "reranker-readiness.json");
await fs.mkdir(path.dirname(file), { recursive: true });
await fs.writeFile(file, `${JSON.stringify(report, null, 2)}\n`, "utf8");
console.log(JSON.stringify(report, null, 2));
