import crypto from "node:crypto";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..", "..");
const publicDir = path.join(here, "public");
const proposalsPath = process.env.KEN_REVIEW_PROPOSALS_PATH || path.join(root, "data", "v02-contextual-review", "proposals.json");
const statePath = process.env.KEN_REVIEW_STATE_PATH || path.join(root, "data", "v02-contextual-review", "decision-state.json");
const learningPath = process.env.KEN_REVIEW_LEARNING_PATH || path.join(root, "knowledge", "contextual-interlinking-decisions.jsonl");
const newDraftsPath = process.env.KEN_REVIEW_NEW_DRAFTS_PATH || path.join(root, "outputs", "contextual-v02-shadow", "new_eligible_interlinking_drafts.json");
const clusterDraftsPath = process.env.KEN_REVIEW_CLUSTER_DRAFTS_PATH || path.join(root, "outputs", "contextual-v02-shadow", "cluster_interlinking_drafts.json");
const topicDraftsPath = process.env.KEN_REVIEW_TOPIC_DRAFTS_PATH || path.join(root, "outputs", "contextual-v02-shadow", "kuwait_topic_cluster_drafts.json");
const topicEvidencePath = process.env.KEN_REVIEW_TOPIC_EVIDENCE_PATH || path.join(root, "outputs", "contextual-v02-shadow", "kuwait_topic_evidence.json");
const liveApplyPath = process.env.KEN_REVIEW_LIVE_APPLY_PATH || path.join(root, "outputs", "contextual-v02-live", "approved-apply-2026-11-20.json");
const topicLivePath = process.env.KEN_REVIEW_TOPIC_LIVE_PATH || path.join(root, "outputs", "contextual-v02-live", "topic-kuwait-1-live-2026-11-20.json");
const topicBatchPath = process.env.KEN_REVIEW_TOPIC_BATCH_PATH || path.join(root, "outputs", "contextual-v02-live", "approved-topic-batch-2026-11-20.json");
const graphSnapshotPath = process.env.KEN_REVIEW_GRAPH_PATH || path.join(root, "outputs", "graph-intelligence", "kuwait-topic-graph-current.json");
const host = "127.0.0.1";
const port = Number.parseInt(process.env.KEN_REVIEW_DASHBOARD_PORT || "4177", 10);

const decisions = new Set(["approve", "revise", "reject"]);
const rejectionReasons = new Set([
  "irrelevant_destination",
  "better_destination_exists",
  "misleading_anchor",
  "unnatural_anchor",
  "anchor_too_generic",
  "wrong_placement",
  "over_linking",
  "duplicate_existing_link",
  "outdated_content",
  "reader_journey_mismatch",
  "regional_relationship_too_weak",
  "destination_not_indexable",
  "destination_content_too_weak",
  "authority_only_without_reader_value",
  "other",
]);

function json(res, status, value) {
  const body = JSON.stringify(value);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  res.end(body);
}

async function readJson(file, fallback) {
  try {
    return JSON.parse(await fs.readFile(file, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") return fallback;
    throw error;
  }
}

async function load() {
  const dataset = await readJson(proposalsPath, null);
  if (!dataset) throw new Error(`Missing dashboard data: run the v0.2 shadow builder first (${proposalsPath}).`);
  const newDrafts = await readJson(newDraftsPath, { results: [] });
  const clusterDrafts = await readJson(clusterDraftsPath, { results: [] });
  const topicDrafts = await readJson(topicDraftsPath, { results: [] });
  const topicEvidence = await readJson(topicEvidencePath, { reports: [] });
  const topicEvidenceBySlug = new Map(topicEvidence.reports.map((report) => [report.slug, report]));
  const liveApply = await readJson(liveApplyPath, { results: [] });
  const topicLive = await readJson(topicLivePath, null);
  const topicBatch = await readJson(topicBatchPath, { results: [] });
  const graph = await readJson(graphSnapshotPath, { nodes: [], edges: [] });
  const liveById = new Map(liveApply.results.map((result) => [result.id, result]));
  if (topicLive?.id) liveById.set(topicLive.id, topicLive);
  for (const result of topicBatch.results ?? []) {
    liveById.set(result.id, { ...result, deployment_ids: result.deployment_id ? [result.deployment_id] : [] });
  }
  const state = await readJson(statePath, { schema_version: 1, decisions: {} });
  const existingIds = new Set(dataset.proposals.map((proposal) => proposal.id));
  const additions = newDrafts.results
    .filter((draft) => draft.verdict === "draft_ready" && !existingIds.has(draft.id))
    .map((draft) => {
      const copyEdit = Boolean(draft.copy_change);
      const deployment = liveById.get(draft.id);
      const live = deployment?.rendered_verified === true;
      const rolledBack = Array.isArray(deployment?.rollback_status) && deployment.rollback_status.every((entry) => entry.status === "rolled_back");
      const scorer = structuredClone(draft.scorer);
      const renderedGate = scorer?.hard_gates?.find((gate) => gate.label === "Rendered crawlable link");
      if (renderedGate && live) renderedGate.status = "pass";
      if (renderedGate && rolledBack) renderedGate.status = "fail";
      if (scorer && live) scorer.missing_information = [];
      if (scorer && rolledBack) scorer.missing_information = ["The CMS canary was rolled back because this approved section did not produce a public rendered anchor."];
      const shortTitle = (value) => String(value).replace(/\s+Market Size[\s\S]*$/i, "");
      return {
        id: draft.id,
        kind: "contextual",
        review_batch: "New evidence audit · 2026-11-20",
        current_status: live ? "Live and publicly verified" : rolledBack ? "Rolled back - public template did not render placement" : "Shadow - awaiting approval",
        change_class: copyEdit ? "Small contextual edit + link" : "Link existing explanatory text",
        mode: copyEdit ? "Evidence-backed small edit" : "Existing text; no copy change",
        source_report: shortTitle(draft.source_report),
        destination_report: shortTitle(draft.destination_report),
        placement: `${draft.source_field}, visible explanatory prose`,
        proposed_wording: copyEdit
          ? `Replace the current paragraph with the proposed linked copy shown below.`
          : `Link the existing phrase “${draft.anchor}”; do not change the surrounding copy.`,
        reader_value: draft.reader_value,
        source_url: draft.source_url,
        destination_url: draft.destination_url,
        deployment_id: deployment?.deployment_ids?.join(", ") || "",
        next_step: live ? "measure_engagement" : rolledBack ? "select_visible_placement" : "contextual_dry_run",
        policy_eligible: true,
        policy_reasons: [],
        anchor: draft.anchor,
        before_copy: draft.before_copy,
        after_copy: draft.after_copy,
        source_evidence: draft.source_evidence,
        target_evidence: draft.target_evidence,
        expected_source_version: draft.source_version,
        cms_validation: live ? "Applied and public anchor verified" : rolledBack ? "Canary applied, failed public rendering and rolled back" : draft.cms_validation,
        scorer,
      };
    });
  const clusterAdditions = clusterDrafts.results
    .filter((draft) => draft.verdict === "draft_ready" && !existingIds.has(draft.id))
    .map((draft) => ({
      id: draft.id,
      kind: "cluster",
      review_batch: "Exact-market cluster audit · 2026-11-20",
      current_status: "Shadow - awaiting approval",
      change_class: "Cluster comparison sentence + link",
      mode: "Exact market, different geography",
      source_report: String(draft.source_report).replace(/\s+Market Size[\s\S]*$/i, ""),
      destination_report: String(draft.destination_report).replace(/\s+Market Size[\s\S]*$/i, ""),
      placement: `${draft.source_field}, after the public forecast paragraph`,
      proposed_wording: draft.proposed_sentence,
      reader_value: draft.reader_value,
      source_url: draft.source_url,
      destination_url: draft.destination_url,
      deployment_id: "",
      next_step: "contextual_dry_run",
      policy_eligible: true,
      policy_reasons: [],
      anchor: draft.anchor,
      market_core: draft.cluster,
      before_copy: draft.exact_after_text,
      after_copy: `${draft.exact_after_text}\n\n${draft.proposed_sentence}`,
      source_evidence: draft.source_evidence,
      target_evidence: draft.destination_evidence,
      expected_source_version: draft.source_version,
      cms_validation: "Not run - approval required first",
      scorer: draft.scorer,
    }));
  const topicAdditions = topicDrafts.results
    .filter((draft) => draft.verdict === "draft_ready" && !existingIds.has(draft.id))
    .map((draft) => {
      const sourceAudit = topicEvidenceBySlug.get(draft.source_slug);
      const destinationAudit = topicEvidenceBySlug.get(draft.destination_slug);
      const deployment = liveById.get(draft.id);
      const live = deployment?.rendered_verified === true;
      const sourceIndexability = sourceAudit?.indexability ?? {};
      const indexability = destinationAudit?.indexability ?? {};
      const destinationPresented = draft.destination_evidence_public_visible === true;
      const hrefMatchesCanonical = Boolean(indexability.canonical_url) && indexability.canonical_url.replace(/\/$/, "") === draft.destination_url.replace(/\/$/, "");
      const authorityBlocked = sourceIndexability.nofollow !== false || indexability.technically_indexable !== true || !hrefMatchesCanonical;
      const scorer = structuredClone(draft.scorer);
      if (live) {
        for (const gate of scorer?.hard_gates ?? []) {
          if (["CMS dry run", "Rendered crawlable link"].includes(gate.label) || (deployment?.current_target_evidence && gate.label === "Destination passage publicly presented")) gate.status = "pass";
        }
        scorer.missing_information = scorer.missing_information.filter((message) => !message.includes("CMS dry run") && !message.includes("rendered verification") && !(deployment?.current_target_evidence && message.includes("Destination evidence")));
      }
      if (deployment?.current_target_evidence) {
        const addedValue = scorer?.kpis?.find((kpi) => kpi.key === "added_value");
        if (addedValue) addedValue.score = addedValue.maximum;
        scorer.total = scorer.kpis.reduce((sum, kpi) => sum + Number(kpi.score || 0), 0);
      }
      return {
      id: draft.id,
      kind: "topic",
      review_batch: "Kuwait topic clusters - 2026-11-20",
      current_status: live ? deployment?.current_target_evidence ? "Live and publicly verified with destination data" : "Live and publicly verified on test host" : "Shadow - editorial review; destination presentation check pending",
      change_class: `${draft.cluster}: short contextual bridge`,
      mode: `Add ${draft.added_word_count} words after existing prose`,
      source_report: String(draft.source_report).replace(/\s+Market Size[\s\S]*$/i, ""),
      destination_report: String(draft.destination_report).replace(/\s+Market Size[\s\S]*$/i, ""),
      source_url: draft.source_url,
      destination_url: draft.destination_url,
      placement: `${draft.source_field}; after the exact public source passage below`,
      proposed_wording: deployment?.current_sentence || draft.proposed_sentence,
      reader_value: draft.reader_value,
      deployment_id: deployment?.deployment_ids?.join(", ") || "",
      next_step: live ? "measure_engagement" : "contextual_dry_run",
      policy_eligible: true,
      policy_reasons: [],
      market_core: draft.cluster,
      anchor: draft.anchor,
      before_copy: draft.before_copy,
      after_copy: deployment?.current_sentence ? `${draft.before_copy}\n\n${deployment.current_sentence}` : draft.after_copy,
      source_evidence: draft.source_evidence,
      target_evidence: deployment?.current_target_evidence ? `${deployment.current_target_evidence}\n\nThis fact was matched in the destination's publicly visible Market Trajectory before the live revision.` : `${draft.destination_evidence}\n\nForecast preview evidence (public): ${draft.destination_preview_evidence || "No separate public forecast fact recorded."}\n\nEvidence location: ${draft.destination_evidence_field}. ${draft.destination_evidence_public_visible ? "Exact relationship passage matched public HTML." : "The relationship passage is CMS-supported; the forecast preview was separately matched in public Market Trajectory."}`,
      expected_source_version: draft.source_version,
      cms_validation: live ? deployment?.current_revision_deployment_id ? `Destination-data revision applied as deployment ${deployment.current_revision_deployment_id}; CMS and public sentence verified` : "Atomic content insertion applied; CMS and public anchor verified" : "Not run; editorial approval and destination presentation check required",
      scorer,
      authority_routing: {
        objective: "Support a useful destination through a relevant contextual link",
        readiness: authorityBlocked ? "blocked" : destinationPresented ? "eligible" : "hold_for_content_check",
        technical_checks: [
          { label: "Source permits link following", status: sourceIndexability.nofollow === false ? "pass" : sourceIndexability.nofollow === true ? "fail" : "pending" },
          { label: `Destination HTTP ${indexability.http_status ?? "unknown"}`, status: indexability.http_status === 200 ? "pass" : "fail" },
          { label: "Destination not blocked by noindex", status: indexability.noindex === false ? "pass" : indexability.noindex === true ? "fail" : "pending" },
          { label: "Proposed href is canonical URL", status: hrefMatchesCanonical ? "pass" : indexability.canonical_url ? "fail" : "pending" },
          { label: "Public destination content presented", status: destinationPresented ? "pass" : "pending" },
        ],
        canonical_url: indexability.canonical_url ?? "Not detected",
        google_index_status: indexability.google_index_status ?? "unknown_without_search_console",
        ranking_status: indexability.ranking_status ?? "unknown_without_search_console",
        note: !authorityBlocked
          ? "Technically eligible to receive internal-link support. Google index and ranking status still require Search Console data; an internal link can help discovery and context, but cannot guarantee indexing or rankings."
          : "Do not route authority here yet. The test host currently uses noindex/nofollow, and the proposed www URL does not match the non-www canonical. Keep this as an editorial draft until a production, followable canonical target is available."
      },
    };
    });
  return { dataset: { ...dataset, proposals: [...topicAdditions, ...clusterAdditions, ...additions, ...dataset.proposals], graph }, state };
}

function proposalMap(dataset) {
  return new Map(dataset.proposals.map((proposal) => [proposal.id, proposal]));
}

async function bodyOf(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 32_768) throw new Error("Request body is too large.");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

function clean(value, limit = 1200) {
  return String(value ?? "").replaceAll("\u0000", "").trim().slice(0, limit);
}

function validateDecision(input, proposals) {
  const proposalId = clean(input.proposal_id, 80);
  const decision = clean(input.decision, 20);
  const reasonCode = clean(input.reason_code, 80);
  const reasonNote = clean(input.reason_note);
  const revisionNote = clean(input.revision_note);
  const proposal = proposals.get(proposalId);
  if (!proposal) return { error: "Unknown proposal." };
  if (!decisions.has(decision)) return { error: "Choose Approve, Revise or Reject." };
  if (decision === "approve" && proposal.policy_eligible === false) {
    return { error: "This proposal is excluded by the current editorial policy and cannot be approved." };
  }
  if (decision === "reject" && !rejectionReasons.has(reasonCode)) return { error: "Choose a rejection reason." };
  if (decision === "reject" && reasonNote.length < 5) return { error: "Explain the rejection so the project can learn from it." };
  if (decision === "revise" && revisionNote.length < 5) return { error: "Describe the revision you want." };
  return { proposal, proposalId, decision, reasonCode, reasonNote, revisionNote };
}

async function persistState(state) {
  await fs.mkdir(path.dirname(statePath), { recursive: true });
  await fs.writeFile(statePath, JSON.stringify(state, null, 2) + "\n", "utf8");
}

async function appendLearning(event) {
  await fs.mkdir(path.dirname(learningPath), { recursive: true });
  await fs.appendFile(learningPath, JSON.stringify(event) + "\n", "utf8");
}

async function recordDecision(req, res) {
  const input = await bodyOf(req);
  const { dataset, state } = await load();
  const checked = validateDecision(input, proposalMap(dataset));
  if (checked.error) return json(res, 422, { error: checked.error });
  const now = new Date().toISOString();
  const previous = state.decisions[checked.proposalId] ?? null;
  const record = {
    proposal_id: checked.proposalId,
    decision: checked.decision,
    reason_code: checked.decision === "reject" ? checked.reasonCode : null,
    reason_note: checked.decision === "reject" ? checked.reasonNote : "",
    revision_note: checked.decision === "revise" ? checked.revisionNote : "",
    queued: checked.decision === "approve" ? Boolean(previous?.queued) : false,
    recorded_at: now,
  };
  state.decisions[checked.proposalId] = record;
  await persistState(state);
  await appendLearning({
    event_id: crypto.randomUUID(),
    event: "editorial_decision_recorded",
    dataset: dataset.dataset,
    recorded_at: now,
    ...record,
    proposal: {
      kind: checked.proposal.kind,
      source_report: checked.proposal.source_report,
      destination_report: checked.proposal.destination_report,
      placement: checked.proposal.placement,
      proposed_wording: checked.proposal.proposed_wording,
      reader_value: checked.proposal.reader_value,
    },
  });
  return json(res, 200, { ok: true, record });
}

async function queueDecision(req, res) {
  const input = await bodyOf(req);
  const proposalId = clean(input.proposal_id, 80);
  const queued = input.queued === true;
  const { dataset, state } = await load();
  const proposal = proposalMap(dataset).get(proposalId);
  const record = state.decisions[proposalId];
  if (!proposal) return json(res, 404, { error: "Unknown proposal." });
  if (proposal.policy_eligible === false) return json(res, 409, { error: "This proposal is excluded by editorial policy." });
  if (!record || record.decision !== "approve") return json(res, 409, { error: "Approve this proposal before queuing it." });
  const now = new Date().toISOString();
  state.decisions[proposalId] = { ...record, queued, recorded_at: now };
  await persistState(state);
  await appendLearning({
    event_id: crypto.randomUUID(),
    event: queued ? "next_step_queued" : "next_step_removed",
    dataset: dataset.dataset,
    proposal_id: proposalId,
    proposal_kind: proposal.kind,
    next_step: proposal.next_step,
    recorded_at: now,
  });
  return json(res, 200, { ok: true, record: state.decisions[proposalId] });
}

async function serveFile(req, res, pathname) {
  const name = pathname === "/" ? "index.html" : pathname.slice(1);
  if (!new Set(["index.html", "styles.css", "app.js"]).has(name)) return false;
  const file = path.join(publicDir, name);
  const body = await fs.readFile(file);
  const contentType = name.endsWith(".html")
    ? "text/html; charset=utf-8"
    : name.endsWith(".css")
      ? "text/css; charset=utf-8"
      : "text/javascript; charset=utf-8";
  res.writeHead(200, {
    "Content-Type": contentType,
    "Content-Length": body.length,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
  });
  res.end(body);
  return true;
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${host}:${port}`);
    if (req.method === "GET" && url.pathname === "/api/proposals") {
      const { dataset, state } = await load();
      return json(res, 200, { ...dataset, decisions: state.decisions });
    }
    if (req.method === "POST" && url.pathname === "/api/decisions") return await recordDecision(req, res);
    if (req.method === "POST" && url.pathname === "/api/queue") return await queueDecision(req, res);
    if (req.method === "GET" && await serveFile(req, res, url.pathname)) return;
    return json(res, 404, { error: "Not found." });
  } catch (error) {
    console.error(error);
    return json(res, 500, { error: error instanceof Error ? error.message : "Unexpected error." });
  }
});

server.listen(port, host, () => {
  console.log(`v0.2 review dashboard: http://${host}:${port}`);
  console.log(`decisions: ${statePath}`);
  console.log(`learning log: ${learningPath}`);
});

