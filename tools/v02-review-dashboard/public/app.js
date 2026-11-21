const state = { data: null, eligibility: "eligible", kind: "all", status: "all", search: "" };
const $ = (selector) => document.querySelector(selector);
const cards = $("#cards");
const dialog = $("#decision-dialog");
const form = $("#decision-form");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[character]);
}

function decisionFor(id) { return state.data?.decisions?.[id] ?? null; }
function label(value) { return ({ approve: "Approved", revise: "Needs revision", reject: "Rejected" })[value] ?? "Awaiting decision"; }
function reasonLabel(value) { return String(value ?? "").replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase()); }

function filtered() {
  const needle = state.search.toLowerCase();
  return state.data.proposals.filter((proposal) => {
    const decision = decisionFor(proposal.id)?.decision ?? "pending";
    const matchesText = !needle || [proposal.id, proposal.review_batch, proposal.source_report, proposal.destination_report, proposal.proposed_wording, proposal.market_core, proposal.reader_value, proposal.before_copy, proposal.after_copy].some((value) => String(value ?? "").toLowerCase().includes(needle));
    const matchesEligibility = state.eligibility === "all" || (state.eligibility === "eligible" ? proposal.policy_eligible : !proposal.policy_eligible);
    return matchesText && matchesEligibility && (state.kind === "all" || proposal.kind === state.kind) && (state.status === "all" || decision === state.status);
  });
}

function updateMetrics() {
  const records = state.data.proposals.map((proposal) => decisionFor(proposal.id));
  $("#metric-total").textContent = records.length;
  $("#metric-eligible").textContent = state.data.proposals.filter((proposal) => proposal.policy_eligible).length;
  $("#metric-excluded").textContent = state.data.proposals.filter((proposal) => !proposal.policy_eligible).length;
  $("#metric-pending").textContent = records.filter((record) => !record).length;
  $("#metric-approved").textContent = records.filter((record) => record?.decision === "approve").length;
  $("#metric-revised").textContent = records.filter((record) => record?.decision === "revise").length;
  $("#metric-rejected").textContent = records.filter((record) => record?.decision === "reject").length;
  $("#metric-queued").textContent = records.filter((record) => record?.queued).length;
}

function scorerHtml(scorer) {
  if (!scorer) return "";
  const total = Math.max(0, Math.min(Number(scorer.maximum) || 100, Number(scorer.total) || 0));
  const maximum = Number(scorer.maximum) || 100;
  const decision = reasonLabel(scorer.decision);
  const kpis = (scorer.kpis ?? []).map((kpi) => {
    const score = Number(kpi.score) || 0;
    const max = Number(kpi.maximum) || 1;
    const width = Math.max(0, Math.min(100, (score / max) * 100));
    return `<div class="kpi-row"><div class="kpi-label"><span>${escapeHtml(kpi.label)}</span><strong>${score}/${max}</strong></div><div class="kpi-track" role="progressbar" aria-label="${escapeHtml(kpi.label)}" aria-valuenow="${score}" aria-valuemin="0" aria-valuemax="${max}"><span style="width:${width}%"></span></div></div>`;
  }).join("");
  const gates = (scorer.hard_gates ?? []).map((gate) => `<span class="gate ${escapeHtml(gate.status)}"><strong>${escapeHtml(gate.status === "pass" ? "Pass" : gate.status === "pending" ? "Pending" : "Fail")}</strong>${escapeHtml(gate.label)}</span>`).join("");
  const confidenceText = scorer.confidence == null ? "Editorial assessment; confidence not calibrated" : `${scorer.confidence}% reviewer confidence`;
  return `<section class="score-panel" aria-label="Contextual interlinking score"><div class="score-panel-head"><div><span class="score-caption">Independent editorial reference</span><strong class="score-total">${total}<small>/${maximum}</small></strong></div><div class="score-verdict"><strong>${escapeHtml(decision)}</strong><span>${escapeHtml(confidenceText)}</span></div></div><div class="overall-track" role="progressbar" aria-label="Overall editorial score" aria-valuenow="${total}" aria-valuemin="0" aria-valuemax="${maximum}"><span style="width:${(total / maximum) * 100}%"></span></div><div class="kpi-list">${kpis}</div><div class="gate-list">${gates}</div><p class="score-disclaimer">${escapeHtml(scorer.disclaimer)} ${escapeHtml((scorer.missing_information ?? []).join(" "))}</p></section>`;
}

function authorityHtml(authority) {
  if (!authority) return "";
  const label = authority.readiness === "eligible" ? "Eligible to support" : authority.readiness === "blocked" ? "Blocked" : "Hold for content check";
  const checks = (authority.technical_checks ?? []).map((check) => `<span class="gate ${escapeHtml(check.status)}"><strong>${escapeHtml(check.status === "pass" ? "Pass" : check.status === "fail" ? "Fail" : "Pending")}</strong>${escapeHtml(check.label)}</span>`).join("");
  const searchStatus = authority.google_index_status === "unknown_without_search_console" ? "Unknown — Search Console not connected" : authority.google_index_status;
  const rankingStatus = authority.ranking_status === "unknown_without_search_console" ? "Unknown — Search Console not connected" : authority.ranking_status;
  return `<section class="authority-panel ${escapeHtml(authority.readiness)}" aria-label="Authority and indexability routing"><div class="authority-head"><div><span class="score-caption">Authority and indexability routing</span><strong>${escapeHtml(label)}</strong></div><span class="tag">Separate hard gate</span></div><div class="gate-list">${checks}</div><dl class="authority-facts"><div><dt>Google index status</dt><dd>${escapeHtml(searchStatus)}</dd></div><div><dt>Ranking status</dt><dd>${escapeHtml(rankingStatus)}</dd></div><div><dt>Canonical</dt><dd>${escapeHtml(authority.canonical_url)}</dd></div></dl><p>${escapeHtml(authority.note)}</p></section>`;
}

function cardHtml(proposal) {
  const decision = decisionFor(proposal.id);
  const decisionName = decision?.decision ?? "pending";
  const note = decision?.decision === "reject"
    ? `<div class="decision-note"><strong>${escapeHtml(reasonLabel(decision.reason_code))}</strong><p>${escapeHtml(decision.reason_note)}</p></div>`
    : decision?.decision === "revise"
      ? `<div class="decision-note"><strong>Requested revision</strong><p>${escapeHtml(decision.revision_note)}</p></div>`
      : "";
  const queueText = proposal.next_step === "regional_module" ? "Queue module build" : "Queue five-page canary";
  const nextStepText = proposal.next_step === "contextual_dry_run" ? "Queue dry-run validation" : queueText;
  const live = String(proposal.current_status).startsWith("Live");
  const policyNote = proposal.policy_eligible ? "" : `<div class="decision-note"><strong>Excluded by current editorial policy</strong><p>${escapeHtml((proposal.policy_reasons ?? []).join(" "))}${live ? " This link is currently live and requires a separate rollback review; it has not been changed automatically." : ""}</p></div>`;
  const evidence = proposal.before_copy ? `<dl class="details evidence-details"><div class="detail wide"><dt>Current source copy</dt><dd>${escapeHtml(proposal.before_copy)}</dd></div><div class="detail wide proposed-copy"><dt>Proposed linked copy</dt><dd>${escapeHtml(proposal.after_copy)}</dd></div><div class="detail wide"><dt>Destination evidence</dt><dd>${escapeHtml(proposal.target_evidence)}</dd></div><div class="detail"><dt>Expected source version</dt><dd>${escapeHtml(proposal.expected_source_version)}</dd></div><div class="detail"><dt>CMS validation</dt><dd>${escapeHtml(proposal.cms_validation === "not_run_shadow_only" ? "Not run — approval required first" : proposal.cms_validation)}</dd></div></dl>` : "";
  const score = scorerHtml(proposal.scorer);
  const authority = authorityHtml(proposal.authority_routing);
  const rolledBack = String(proposal.current_status).startsWith("Rolled back");
  const copyRule = Array.isArray(proposal.copy_rule_problems)
    ? `<div class="gate-list copy-rule">${proposal.copy_rule_problems.length === 0 ? '<span class="gate pass"><strong>Pass</strong>Copy rule: 2+ sentences, 32-38 words, no dashes, no reader address</span>' : proposal.copy_rule_problems.map((problem) => `<span class="gate fail"><strong>Fail</strong>Copy rule: ${escapeHtml(problem)}</span>`).join("")}</div>`
    : "";
  return `<article class="card" data-decision="${escapeHtml(decisionName)}">
    <div class="card-top"><div><div class="card-kicker"><span class="tag ${proposal.kind}">${proposal.kind === "regional" ? "Regional sibling" : proposal.kind === "cluster" ? "Exact-market cluster" : proposal.kind === "relocated" ? "Relocated after rollback" : "Contextual link"}</span>${proposal.review_batch ? '<span class="tag new-batch">New draft</span>' : ""}${live ? '<span class="tag approve">Live &amp; verified</span>' : ""}${rolledBack ? '<span class="tag reject">Rolled back</span>' : ""}<span class="tag ${proposal.policy_eligible ? "" : "excluded"}">${proposal.policy_eligible ? "Eligible for review" : "Excluded"}</span><span class="tag ${escapeHtml(decisionName)}">${escapeHtml(label(decisionName))}</span>${decision?.queued ? '<span class="tag approve">Queued next</span>' : ""}</div><h3>${escapeHtml(proposal.change_class)}</h3></div><span class="tag">${escapeHtml(proposal.id)}</span></div>
    <div class="route"><div><span>Source</span><strong>${escapeHtml(proposal.source_report)}</strong></div><div class="route-separator" aria-hidden="true">to</div><div><span>Destination</span><strong>${escapeHtml(proposal.destination_report)}</strong></div></div>
    <dl class="details"><div class="detail"><dt>Placement</dt><dd>${escapeHtml(proposal.placement)}</dd></div><div class="detail"><dt>Mode</dt><dd>${escapeHtml(proposal.mode)}</dd></div><div class="detail wide"><dt>Anchor or proposed wording</dt><dd>${escapeHtml(proposal.proposed_wording)}</dd></div><div class="detail wide"><dt>Why it helps the reader</dt><dd>${escapeHtml(proposal.reader_value)}</dd></div></dl>
    ${copyRule}${score}${authority}${evidence}${policyNote}${note}
    <div class="card-actions"><button class="button primary decide" data-id="${escapeHtml(proposal.id)}" type="button">${decision ? "Change decision" : "Record decision"}</button>${proposal.policy_eligible && decision?.decision === "approve" && !live && String(proposal.current_status).startsWith("Shadow") ? `<button class="button ${decision.queued ? "secondary" : "queue"} queue-action" data-id="${escapeHtml(proposal.id)}" data-queued="${decision.queued ? "false" : "true"}" type="button">${decision.queued ? "Remove from queue" : nextStepText}</button>` : ""}<a class="source-link" href="${escapeHtml(proposal.source_url)}" target="_blank" rel="noreferrer">Open source</a><a class="source-link" href="${escapeHtml(proposal.destination_url)}" target="_blank" rel="noreferrer">Open destination</a></div>
  </article>`;
}

function render() {
  updateMetrics();
  renderGraph();
  const rows = filtered();
  $("#queue-count").textContent = `${rows.length} of ${state.data.proposals.length} proposals shown`;
  cards.innerHTML = rows.length ? rows.map(cardHtml).join("") : '<div class="empty">No proposals match these filters.</div>';
  document.querySelectorAll(".decide").forEach((button) => button.addEventListener("click", () => openDecision(button.dataset.id)));
  document.querySelectorAll(".queue-action").forEach((button) => button.addEventListener("click", () => queue(button.dataset.id, button.dataset.queued === "true")));
}

function renderGraph() {
  const graph = state.data?.graph;
  const svg = $("#link-graph");
  if (!svg || !graph?.nodes?.length) return;
  const select = $("#graph-group");
  if (select.options.length === 1) {
    [...new Set(graph.nodes.map((node) => node.group))].sort().forEach((group) => select.add(new Option(group, group)));
    select.addEventListener("change", renderGraph);
  }
  const group = select.value;
  const nodes = graph.nodes.filter((node) => group === "all" || node.group === group);
  const ids = new Set(nodes.map((node) => node.id));
  const edges = graph.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target));
  const width = 1100, height = 620, centerX = width / 2, centerY = height / 2;
  const byGroup = new Map();
  for (const node of nodes) { if (!byGroup.has(node.group)) byGroup.set(node.group, []); byGroup.get(node.group).push(node); }
  const groups = [...byGroup].sort(([a], [b]) => a.localeCompare(b));
  const positions = new Map();
  groups.forEach(([name, members], groupIndex) => {
    const groupAngle = (Math.PI * 2 * groupIndex) / Math.max(1, groups.length) - Math.PI / 2;
    const groupRadius = groups.length === 1 ? 0 : 210;
    const gx = centerX + Math.cos(groupAngle) * groupRadius;
    const gy = centerY + Math.sin(groupAngle) * groupRadius;
    members.sort((a, b) => a.id.localeCompare(b)).forEach((node, index) => {
      const angle = (Math.PI * 2 * index) / Math.max(1, members.length) - Math.PI / 2;
      const radius = members.length === 1 ? 0 : Math.min(88, 34 + members.length * 7);
      positions.set(node.id, { x: gx + Math.cos(angle) * radius, y: gy + Math.sin(angle) * radius });
    });
  });
  const lineMarkup = edges.map((edge) => { const a = positions.get(edge.source), b = positions.get(edge.target); return `<line class="graph-edge ${escapeHtml(edge.kind)}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"><title>${escapeHtml(edge.kind)}: ${escapeHtml(edge.anchor || "link")}</title></line>`; }).join("");
  const nodeMarkup = nodes.map((node) => { const p = positions.get(node.id); const radius = 7 + Math.min(9, Number(node.inbound || 0) * 2); return `<a href="${escapeHtml(node.url)}" target="_blank" rel="noreferrer"><circle class="graph-node ${node.orphan ? "orphan" : ""}" cx="${p.x}" cy="${p.y}" r="${radius}"><title>${escapeHtml(node.title)} | inbound ${node.inbound} | outbound ${node.outbound}</title></circle></a>`; }).join("");
  svg.innerHTML = `<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker></defs>${lineMarkup}${nodeMarkup}`;
  const liveCount = edges.filter((edge) => edge.kind === "live").length;
  const orphanCount = nodes.filter((node) => node.orphan).length;
  $("#graph-summary").textContent = `${nodes.length} reports · ${edges.length} visible/proposed connections · ${liveCount} live topic links · ${orphanCount} subset orphans`;
}

function setDecisionFields() {
  const decision = form.elements.decision.value;
  $("#revision-fields").hidden = decision !== "revise";
  $("#rejection-fields").hidden = decision !== "reject";
}

function openDecision(id) {
  const proposal = state.data.proposals.find((item) => item.id === id);
  const existing = decisionFor(id);
  form.reset();
  $("#proposal-id").value = id;
  $("#dialog-title").textContent = `${proposal.source_report} → ${proposal.destination_report}`;
  $("#form-error").hidden = true;
  if (existing) {
    const radio = form.querySelector(`input[name="decision"][value="${existing.decision}"]`);
    if (radio) radio.checked = true;
    $("#reason-code").value = existing.reason_code ?? "";
    $("#reason-note").value = existing.reason_note ?? "";
    $("#revision-note").value = existing.revision_note ?? "";
  }
  setDecisionFields();
  dialog.showModal();
}

async function request(url, payload) {
  const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The request could not be saved.");
  return result;
}

async function saveDecision(event) {
  event.preventDefault();
  const decision = form.elements.decision.value;
  const error = $("#form-error");
  error.hidden = true;
  if (!decision) { error.textContent = "Choose Approve, Revise or Reject."; error.hidden = false; return; }
  $("#save-decision").disabled = true;
  try {
    await request("/api/decisions", { proposal_id: $("#proposal-id").value, decision, reason_code: $("#reason-code").value, reason_note: $("#reason-note").value, revision_note: $("#revision-note").value });
    dialog.close();
    await load();
    toast("Decision recorded in the project learning log.");
  } catch (failure) { error.textContent = failure.message; error.hidden = false; }
  finally { $("#save-decision").disabled = false; }
}

async function queue(id, queued) {
  try { await request("/api/queue", { proposal_id: id, queued }); await load(); toast(queued ? "Approved item queued for the next controlled step." : "Item removed from the next-step queue."); }
  catch (failure) { showError(failure.message); }
}

function toast(message) { const element = $("#toast"); element.textContent = message; element.hidden = false; window.setTimeout(() => { element.hidden = true; }, 3200); }
function showError(message) { const element = $("#error"); element.textContent = message; element.hidden = false; }

async function load() {
  $("#error").hidden = true;
  const response = await fetch("/api/proposals", { cache: "no-store" });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Could not load proposals.");
  state.data = result;
  render();
}

document.querySelectorAll('input[name="kind"]').forEach((input) => input.addEventListener("change", () => { state.kind = input.value; render(); }));
document.querySelectorAll('input[name="eligibility"]').forEach((input) => input.addEventListener("change", () => { state.eligibility = input.value; render(); }));
document.querySelectorAll('input[name="status"]').forEach((input) => input.addEventListener("change", () => { state.status = input.value; render(); }));
$("#search").addEventListener("input", (event) => { state.search = event.target.value; render(); });
$("#refresh").addEventListener("click", () => load().catch((error) => showError(error.message)));
form.addEventListener("change", setDecisionFields);
form.addEventListener("submit", saveDecision);
load().catch((error) => showError(error.message));

