"""Phase 2 content intelligence (ADR-0010).

Deterministic analysis core -- pure functions, no database, no network, no LLM:

* ``text``            folding, whole-phrase matching, keyword tokens, truncation
* ``taxonomy``        policy loading (cached) and the precompiled TaxonomyIndex
* ``analyze``         AnalysisInput -> PageAnalysis
* ``protection``      policy protection rules
* ``eligibility``     read-time LinkEligibility
* ``relationships``   taxonomy and semantic relationship candidates
* ``embedding_text``  the embedded text recipe and its input hash
"""
