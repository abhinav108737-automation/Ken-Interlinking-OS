import fs from "node:fs/promises";
import path from "node:path";

const inputFile = process.argv[2];
if (!inputFile) throw new Error("Usage: rrf-shadow-eval.mjs INPUT.json [OUTPUT.json]");
const input = JSON.parse(await fs.readFile(path.resolve(inputFile), "utf8"));
const rankConstant = Number(input.rank_constant || 60);
const judgments = new Map((input.judgments || []).map((item) => [`${item.query_id}\u0000${item.candidate_id}`, Number(item.relevance)]));
const results = [];
for (const query of input.queries || []) {
  const scores = new Map();
  const ranks = new Map();
  for (const channel of Object.keys(query.channels || {}).sort()) {
    const seen = new Set();
    (query.channels[channel] || []).forEach((candidate, index) => {
      if (seen.has(candidate)) return;
      seen.add(candidate);
      scores.set(candidate, (scores.get(candidate) || 0) + 1 / (rankConstant + index + 1));
      if (!ranks.has(candidate)) ranks.set(candidate, {});
      ranks.get(candidate)[channel] = index + 1;
    });
  }
  const ranked = [...scores].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([candidate_id, score]) => ({ candidate_id, score, ranks: ranks.get(candidate_id), relevance: judgments.get(`${query.query_id}\u0000${candidate_id}`) ?? null }));
  const relevant = new Set(ranked.filter((item) => item.relevance === 1).map((item) => item.candidate_id));
  const precision = (k) => ranked.slice(0, k).filter((item) => relevant.has(item.candidate_id)).length / k;
  const recall = (k) => relevant.size ? ranked.slice(0, k).filter((item) => relevant.has(item.candidate_id)).length / relevant.size : 0;
  results.push({ query_id: query.query_id, ranked, precision_at_5: precision(5), precision_at_10: precision(10), recall_at_20: recall(20) });
}
const report = { schema_version: 1, mode: "shadow_only", generated_at: new Date().toISOString(), rank_constant: rankConstant, query_count: results.length, results };
const outputFile = path.resolve(process.argv[3] || "outputs/ranking-eval/rrf-shadow-results.json");
await fs.mkdir(path.dirname(outputFile), { recursive: true });
await fs.writeFile(outputFile, `${JSON.stringify(report, null, 2)}\n`, "utf8");
console.log(JSON.stringify({ output: outputFile, queries: results.length }, null, 2));
