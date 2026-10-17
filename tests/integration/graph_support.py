"""A small link graph whose every metric can be worked out by hand.

    /            -> a, b                (the home page: the crawl entry)
    /reports/a   -> b
    /reports/b   -> c
    /reports/c   -> (nothing)
    /reports/d   -> a                   (nobody links to d: an orphan)
    /reports/e   -> (nothing)           (isolated)

Distinct edges: /->a, /->b, a->b, b->c, d->a.  Industry/market (from the analyser): a, b, c, d
are market ``ev`` (industry ``automotive``); e is ``batteries``; the home page has none.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Final

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import TRUNCATE_ALL, SyncDb, make_link, url
from tests.integration.intelligence_support import analyse, page, page_ids, seed

SM = async_sessionmaker[AsyncSession]

PATHS: Final = ("/", "/reports/a", "/reports/b", "/reports/c", "/reports/d", "/reports/e")


def pages() -> list[object]:
    return [
        page(
            "/",
            "home navigation page words",
            links=(make_link(0, url("/reports/a")), make_link(1, url("/reports/b"))),
        ),
        page(
            "/reports/a",
            "alpha alpha alpha beta market:ev",
            links=(make_link(0, url("/reports/b")),),
        ),
        page(
            "/reports/b",
            "alpha alpha beta beta market:ev",
            links=(make_link(0, url("/reports/c")),),
        ),
        page("/reports/c", "gamma gamma gamma delta market:ev"),
        page(
            "/reports/d",
            "delta delta delta gamma market:ev",
            links=(make_link(0, url("/reports/a")),),
        ),
        page("/reports/e", "gamma delta delta delta market:batteries"),
    ]


async def build(sm: SM) -> dict[str, uuid.UUID]:
    await seed(sm, *pages())
    await analyse(sm)
    return await page_ids(sm)


@pytest.fixture
def leave_clean(sync_db: SyncDb) -> Iterator[None]:
    """Empty every application table AFTER the test.

    The shared ``migrated`` fixture truncates at the START of a test, so rows of the newer job
    kinds (``graph_metrics``, ``vector_index``) would otherwise survive into whichever test
    runs next -- and the migration round trip cannot downgrade past a kind the older CHECK does
    not know. A test must not leave its fingerprints for the next one.
    """
    yield
    sync_db.run(lambda connection: connection.execute(text(TRUNCATE_ALL)))
