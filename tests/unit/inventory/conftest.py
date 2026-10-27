"""Safety net: no inventory test may start a real browser unless it is a marked browser test."""

from __future__ import annotations

import pytest

from ken_services.inventory.sources.browser_render import BrowserRenderer


@pytest.fixture(autouse=True)
def _no_real_browser(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("browser") is not None:
        return

    async def refuse(self: BrowserRenderer) -> None:
        raise AssertionError("a test tried to start a real browser (mark it @pytest.mark.browser)")

    monkeypatch.setattr(BrowserRenderer, "start", refuse)
