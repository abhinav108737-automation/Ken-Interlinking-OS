# Suite: critic

> Phase 0: structure only, with no cases. First populated in **Phase 4** (Critic
> node); formal dataset in Phase 8. TRD §19, §22 ("recorded fixtures for … critic").

**Exercises:** `CRITIQUE` (Critic agent, TRD §10). The critic evaluates plan
quality but **cannot publish**.

**A passing case:** the critic's verdict on a recorded `LinkPlan` matches the
expected verdict, and when it rejects a plan its reason identifies the actual
defect.

**Example cases (prose):**
- A well-formed plan with a natural anchor and a relevant destination should
  be accepted.
- A plan whose anchor overstates the destination's scope should be flagged.
- A plan that would push the source page past its link-density limit should be
  flagged, even though the limit itself is enforced deterministically
  (TRD §11).

**Failure modes guarded:** rubber-stamping, i.e. a critic that accepts
everything; over-rejection that starves the review queue; correct verdicts
given for the wrong reasons; any critic output that attempts a mutation or
bypasses `POLICY_GATE`. Once Phase 5 feedback exists, report precision and
recall against human review outcomes.
