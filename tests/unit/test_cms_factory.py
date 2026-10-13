"""build_cms_provider: the frozen factory the API imports."""

from __future__ import annotations

from ken_contracts.cms import CMSProvider
from ken_core.config import Settings
from ken_core.providers.cms import SEED_PAGE_IDS, MockCMSProvider, build_cms_provider
from ken_core.providers.cms.seed import RESEARCH_METHODOLOGY_PAGE_ID


def _settings(**overrides: object) -> Settings:
    # _env_file=None: tests must not depend on a developer's local .env
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_default_settings_build_mock_provider() -> None:
    provider = build_cms_provider(_settings())
    assert isinstance(provider, MockCMSProvider)
    assert isinstance(provider, CMSProvider)
    assert provider.name == "mock"


async def test_factory_provider_is_seeded() -> None:
    provider = build_cms_provider(_settings(cms_provider="mock"))
    for page_id in SEED_PAGE_IDS:
        page = await provider.get_page(page_id=page_id)
        assert page.url.startswith("https://example.com/")
    assert (await provider.get_page(page_id=RESEARCH_METHODOLOGY_PAGE_ID)).protected is True
    health = await provider.health()
    assert health.reachable is True
    assert health.provider == "mock"
    await provider.aclose()


def test_each_call_returns_independent_provider() -> None:
    settings = _settings()
    assert build_cms_provider(settings) is not build_cms_provider(settings)


def test_seed_pages_use_example_domain_only() -> None:
    from ken_core.providers.cms.seed import seed_pages

    for page in seed_pages():
        assert page.url.startswith("https://example.com/")
        assert page.canonical_url is None or page.canonical_url.startswith("https://example.com/")
        assert "kenresearch" not in page.model_dump_json().lower()
