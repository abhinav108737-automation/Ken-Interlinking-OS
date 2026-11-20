import fs from "node:fs/promises";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const drafts = JSON.parse(await fs.readFile(path.join(ROOT, "outputs", "contextual-v02-shadow", "kuwait_topic_cluster_drafts.json"), "utf8"));
const state = JSON.parse(await fs.readFile(path.join(ROOT, "data", "v02-contextual-review", "decision-state.json"), "utf8"));
const firstLive = JSON.parse(await fs.readFile(path.join(ROOT, "outputs", "contextual-v02-live", "topic-kuwait-1-live-2026-11-20.json"), "utf8"));
const batch = JSON.parse(await fs.readFile(path.join(ROOT, "outputs", "contextual-v02-live", "approved-topic-batch-2026-11-20.json"), "utf8"));
const live = new Map([[firstLive.id, firstLive.rendered_verified === true], ...(batch.results || []).map((item) => [item.id, item.rendered_verified === true])]);
const judgments = drafts.results.flatMap((draft) => {
  const decision = state.decisions?.[draft.id];
  if (!decision) return [];
  return [{
    id: draft.id,
    query_id: draft.source_slug,
    candidate_id: draft.destination_slug,
    decision: decision.decision,
    relevance: decision.decision === "approve" ? 1 : 0,
    reason_code: decision.reason_code,
    contextual_score: draft.scorer.total,
    rendered_live: live.get(draft.id) === true,
    source_field: draft.source_field,
    anchor: draft.anchor,
    evidence_version: draft.source_version,
  }];
});
const output = {
  schema_version: 1,
  generated_at: new Date().toISOString(),
  provenance: "Real Ken dashboard decisions; no synthetic labels or clicks.",
  judgments,
};
const file = path.join(ROOT, "evals", "datasets", "kuwait-topic-real-judgments.json");
await fs.mkdir(path.dirname(file), { recursive: true });
await fs.writeFile(file, `${JSON.stringify(output, null, 2)}\n`, "utf8");
console.log(JSON.stringify({ file: path.relative(ROOT, file), judgments: judgments.length, positive: judgments.filter((item) => item.relevance === 1).length, negative: judgments.filter((item) => item.relevance === 0).length, live: judgments.filter((item) => item.rendered_live).length }, null, 2));
