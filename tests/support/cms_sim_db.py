"""The DISPOSABLE simulated-CMS database every integration test uses (ADR-0014 s8).

``KEN_TEST_CMS_SIM_DATABASE_URL`` overrides the default ``ken_cms_sim_test`` on the project
PostgreSQL (127.0.0.1:5433). Tests EMPTY this database, so the name must start with
``ken_cms_sim`` (the provider's own guard) AND end with ``_test`` (like
``KEN_TEST_DATABASE_URL``): ``ken_cms_sim`` itself, or any other non-test simulated CMS, is
refused before any connection is made.
"""

from __future__ import annotations

import os
from typing import Final

from sqlalchemy.engine import make_url

__all__ = [
    "CMS_SIM_DB_PREFIX",
    "CMS_SIM_TEST_DATABASE_ENV",
    "DEFAULT_CMS_SIM_TEST_DATABASE_URL",
    "cms_sim_test_database_url",
]

CMS_SIM_TEST_DATABASE_ENV: Final = "KEN_TEST_CMS_SIM_DATABASE_URL"
DEFAULT_CMS_SIM_TEST_DATABASE_URL: Final = (
    "postgresql+asyncpg://ken:ken@127.0.0.1:5433/ken_cms_sim_test"
)
CMS_SIM_DB_PREFIX: Final = "ken_cms_sim"


def cms_sim_test_database_url() -> str:
    """The configured simulated-CMS TEST database URL, or ``RuntimeError`` (never echoes it:
    it may carry credentials)."""
    url = os.environ.get(CMS_SIM_TEST_DATABASE_ENV) or DEFAULT_CMS_SIM_TEST_DATABASE_URL
    try:
        database = make_url(url).database or ""
    except Exception:
        raise RuntimeError(f"{CMS_SIM_TEST_DATABASE_ENV} is not a valid URL") from None
    if not (database.startswith(CMS_SIM_DB_PREFIX) and database.endswith("_test")):
        raise RuntimeError(
            f"{CMS_SIM_TEST_DATABASE_ENV} must name a database starting with "
            f"{CMS_SIM_DB_PREFIX!r} and ending in '_test' (tests empty it)"
        )
    return url
