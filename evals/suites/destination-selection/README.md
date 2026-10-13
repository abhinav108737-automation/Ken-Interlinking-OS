# Suite: destination-selection

> Phase 0: structure only, with no cases. First populated in **Phase 3**
> (deterministic ranking baseline) and extended in **Phase 4** (Discovery agent
> fixtures). It becomes a formal dataset in Phase 8. TRD §19.

**Exercises:** `RETRIEVE` → `FILTER` → `RANK` (Discovery agent plus the
ranking policy, TRD §10–§12).

**A passing case:** the expected destination is in the top-k ranked
candidates, no `must_not` destination is selected, and every selected
destination passed deterministic eligibility (PRD §7.5).

**Example cases (prose):**
- A country-level market report should rank its regional parent-market report
  above a semantically similar page from an unrelated industry.
- A candidate the source already links to must not be selected again.
- A strong semantic match that is unpublished or has a non-canonical URL must
  be filtered before ranking.

**Failure modes guarded:** topical drift, i.e. ranking on embeddings alone
while ignoring taxonomy and geography; duplicate links; self-links; ineligible
destinations that leak past `FILTER`; ranking weights hardcoded instead of
read from versioned policy data (TRD §12).
