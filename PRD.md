# Ken Research Agentic Interlinking OS
## Product Requirements Document — v1.0

## 1. Product Vision

Build a fully self-hosted autonomous AI system that continuously manages and improves internal contextual body links across Ken Research pages.

The system must act like an AI employee that can observe website changes, understand page meaning, identify interlinking opportunities, select destinations and anchors, prioritize work, request approval when required, execute approved actions safely, verify production state, measure outcomes, learn from human decisions and analytics, optimize future decisions, and progressively earn execution authority.

The long-term target is not an SEO recommendation dashboard. It is a closed-loop autonomous interlinking operating system.

## 2. Confirmed Product Decisions

- Website/CMS: custom.
- Link placement: body/contextual links only.
- Publishing access: controlled API.
- Analytics: GA4 + Google Search Console + first-party events.
- Autonomy: staged.
- Runtime: standalone/self-hosted.
- Paid LLM APIs: prohibited by default.
- Local inference: required.
- Human approval: mandatory during learning stage.

## 3. Primary Users

### SEO / Content Manager
Reviews recommendations, approves/rejects changes, configures policies, inspects outcomes.

### Content Editor
Reviews anchor, context, destination and placement; supplies feedback labels.

### Administrator / Engineer
Configures CMS integration, models, infrastructure, policies, authentication and recovery.

### Agent Supervisor
Autonomous software actor that maintains the work queue, routes tasks, makes recommendations and coordinates execution.

## 4. Core Product Loop

OBSERVE → UNDERSTAND → DISCOVER → RANK → PLACE → CRITIQUE → POLICY GATE → APPROVE/EXECUTE → VERIFY → MEASURE → LEARN → OPTIMIZE → REPEAT

## 5. Product Goals

### G1 — Improve content discovery
Users should find relevant research directly from the pages they are reading.

### G2 — Reduce orphan and underlinked pages
Important research pages should gain relevant contextual inbound links.

### G3 — Improve engagement
Measure incremental internal-link CTR, downstream page consumption, session depth and qualified commercial journeys.

### G4 — Improve content-graph quality
Strengthen market, geography, industry and commercial relationships across the site.

### G5 — Build proprietary decision data
Persist source, destination, context, anchor, features, agent decision, human decision and eventual outcome.

### G6 — Progressively reduce manual work
Move from approval-required to selective autopilot only when evidence is sufficient.

## 6. Non-Goals for v1

- full article rewriting;
- navigation/footer modification;
- URL restructuring;
- autonomous deletion of content;
- external backlink acquisition;
- automatic policy/model promotion;
- guaranteed ranking improvement;
- unrestricted browser/computer control.

## 7. Functional Requirements

### 7.1 Content Inventory
Ingest page metadata, body HTML/text, content hash, canonical URL, page type, publication state, existing internal links and crawl state.

### 7.2 Content Understanding
For every page identify market, industry, subindustry, geography, region, companies, technologies, products, intent, page type and commercial importance.

### 7.3 Knowledge Graph
Maintain explainable relationship types including SAME_MARKET, PARENT_MARKET, SUBMARKET, RELATED_MARKET, SAME_GEOGRAPHY, REGIONAL_PARENT, SUPPLY_CHAIN_UPSTREAM, SUPPLY_CHAIN_DOWNSTREAM, COMPLEMENTARY_MARKET and INDUSTRY_ADJACENCY.

### 7.4 Candidate Generation
Candidates come from semantic similarity, taxonomy, graph relationships, geography, business priority, underlink opportunity and historical behavior where available.

### 7.5 Deterministic Eligibility
Reject candidates before LLM reasoning if destination is not publishable, canonical/HTTP is invalid, source equals destination, an appropriate link already exists, source/destination is protected or an active experiment forbids modification.

### 7.6 Ranking
Initial ranking is policy-based and configurable. Long-term ranking can become supervised/learning-to-rank.

### 7.7 Anchor and Placement
Prefer existing natural language. Never fabricate source text. Every plan must include paragraph identity and deterministic offsets or another stable selector.

### 7.8 LinkPlan
Every proposed change stores recommendation ID, source, destination, relationship type, anchor, placement, rationale, score breakdown, calibrated confidence, policy version, model version, agent version and source content version.

### 7.9 Human Review
Reviewer may approve, reject, edit anchor, edit destination or edit placement. Rejections/edits require structured reason codes.

### 7.10 Publishing
All approved mutations pass through a CMS integration API with dry-run validation, optimistic locking, idempotency, version checks, audit log and rollback.

### 7.11 Verification
Post-publish verification includes API/HTML checks and optional browser verification for ambiguous/high-value cases.

### 7.12 Analytics
Capture internal_link_impression and internal_link_click plus source/destination/link ID, policy version and experiment ID. Import GA4 and GSC metrics.

## 8. Learning Requirements

### Editorial Learning
Learn from approvals, rejections, edits and replacements.

### Behavioral Learning
Learn from impressions, CTR, next page, session depth and conversion.

### Search Learning
Learn from GSC impressions, clicks, queries and landing-page changes.

## 9. Skills

Procedural skills are versioned reusable operating instructions such as find_parent_market_links, select_natural_anchor, detect_overlinking, repair_orphan_report and evaluate_underperforming_link.

Skills may be proposed or revised from experience, but activation must pass evaluation.

## 10. Policies

Policy lifecycle:
PROPOSED → OFFLINE_TEST → SHADOW → CANARY → EXPERIMENT → PROMOTED

or REJECTED / ROLLED_BACK.

No agent can directly promote a policy to production.

## 11. Autonomy Stages

### Stage 0 — Shadow
No publishing.

### Stage 1 — Human Approval
Every proposed production change requires approval.

### Stage 2 — Selective Autopilot
Only proven low-risk decision classes can publish automatically.

### Stage 3 — Policy Autonomy
Normal low-risk operations are autonomous; humans manage policies, exceptions and experiments.

## 12. MVP Acceptance Criteria

MVP is complete when it can ingest pages, classify pages, generate/store embeddings locally, create content relationships, retrieve candidates, generate contextual recommendations, explain recommendations, collect approvals/rejections, publish through a controlled mock or real CMS API, verify publication, track link-level outcomes, store feedback, run with local models and run without mandatory paid APIs.

## 13. Success Condition

The product succeeds when Ken Research can safely delegate routine interlinking operations to the system while retaining explainability, auditability, reversibility, measurement and policy-level control.
