"""Analytics providers (Phase 7, ADR-0015 §4 as amended by §12).

Every provider implements the frozen :class:`~ken_contracts.analytics.AnalyticsBatchProvider`
and is built by :func:`~ken_core.providers.analytics.factory.build_analytics_provider`.

``ken_core`` never imports ``ken_services``, so a provider refuses with
:class:`~ken_core.providers.analytics.base.AnalyticsFetchError` (a fixed code string, never
row contents, a URL or a token); the importer maps that code to its own vocabulary.
"""
