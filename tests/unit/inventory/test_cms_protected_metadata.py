"""Phase 2: a CMS-protected page carries ``cms_protected: "true"`` assertion metadata.

That is what the taxonomy policy's ``cms-protected`` rule matches (ADR-0010 s.5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from ken_contracts.cms import CMSPage
from ken_contracts.inventory import SourceDocument
from ken_services.inventory.sources.cms import (
    CMS_PROTECTED_KEY,
    CMSContentSource,
    MockCMSInventoryReader,
)

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
TAXONOMY = Path(__file__).resolve().parents[3] / "policies" / "taxonomy" / "taxonomy.v1.yaml"


def _page(page_id: str, **overrides: object) -> CMSPage:
    values: dict[str, object] = {
        "page_id": page_id,
        "url": f"https://example.com/{page_id}",
        "title": page_id,
        "body_html": "<p>Body</p>",
        "version": "v1",
    } | overrides
    return CMSPage.model_validate(values)


async def _metadata(*pages: CMSPage) -> dict[str, dict[str, str]]:
    source = CMSContentSource(
        MockCMSInventoryReader(list(pages)), site_url="https://example.com", clock=lambda: NOW
    )
    await source.site_scope()
    docs = [record async for record in source.iter_records()]
    assert all(isinstance(doc, SourceDocument) for doc in docs)
    return {
        doc.source_id: dict(doc.assertions.metadata)
        for doc in docs
        if isinstance(doc, SourceDocument)
    }


async def test_protected_page_gets_cms_protected_true() -> None:
    metadata = await _metadata(
        _page("locked", protected=True, metadata={"author": "Research"}),
        _page("open", metadata={"author": "Research"}),
    )
    assert metadata["locked"] == {"author": "Research", CMS_PROTECTED_KEY: "true"}
    assert metadata["open"] == {"author": "Research"}


async def test_cms_flag_wins_over_a_conflicting_metadata_key() -> None:
    metadata = await _metadata(
        _page("locked", protected=True, metadata={CMS_PROTECTED_KEY: "false"}),
    )
    assert metadata["locked"][CMS_PROTECTED_KEY] == "true"


def test_key_and_value_match_the_taxonomy_cms_protected_rule() -> None:
    policy = yaml.safe_load(TAXONOMY.read_text(encoding="utf-8"))
    rules = {rule["id"]: rule for rule in policy["protections"]}
    assert rules["cms-protected"]["metadata_equals"] == {CMS_PROTECTED_KEY: "true"}
