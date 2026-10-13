"""Deterministic domain services (TRD.md section 11).

Reserved by docs/adr/ADR-0001.md; populated from Phase 1. Code here never calls
a model: URL normalisation, parsing, hashing, crawling and ingestion are all
deterministic. Dependencies point inward only: ``ken_services`` may import
``ken_core`` and ``ken_contracts``; neither may import ``ken_services``.
"""
