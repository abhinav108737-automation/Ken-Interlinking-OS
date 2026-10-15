"""``assert_safe_sim_url``: the simulated CMS may only ever use a disposable project-local
database. Pure: nothing here connects to anything."""

from __future__ import annotations

from typing import Final

import pytest

from ken_core.config import ConfigurationError
from ken_core.providers.cms.simulated import SimulatedCMSProvider, assert_safe_sim_url

PASSWORD: Final = "pw-Sim-4471"


def _url(rest: str, *, driver: str = "postgresql+asyncpg") -> str:
    return f"{driver}://ken:{PASSWORD}@{rest}"


ACCEPTED: Final = {
    "project_port": _url("127.0.0.1:5433/ken_cms_sim"),
    "test_database": _url("127.0.0.1:5433/ken_cms_sim_test"),
    "localhost": _url("localhost:5433/ken_cms_sim_e2e"),
    "ipv6_loopback": _url("[::1]:5433/ken_cms_sim"),
    "harmless_query": _url("127.0.0.1:5433/ken_cms_sim_test?ssl=disable"),
}

REFUSED: Final = {
    "native_port": _url("127.0.0.1:5432/ken_cms_sim"),
    "default_port_is_native": _url("127.0.0.1/ken_cms_sim"),
    "query_port_override": _url("127.0.0.1:5433/ken_cms_sim?port=5432"),
    "remote_host": _url("db.example.com:5433/ken_cms_sim"),
    "remote_ip": _url("10.0.0.7:5433/ken_cms_sim"),
    "query_host_override": _url("127.0.0.1:5433/ken_cms_sim?host=db.example.com"),
    "multi_host_one_remote": _url(
        "/ken_cms_sim?host=127.0.0.1:5433&host=db.example.com:5433", driver="postgresql+asyncpg"
    ),
    "multi_host_one_native": _url("/ken_cms_sim?host=127.0.0.1:5433&host=localhost:5432"),
    "app_database": _url("127.0.0.1:5433/ken_interlink"),
    "app_test_database": _url("127.0.0.1:5433/ken_interlink_test"),
    "query_database_override": _url("127.0.0.1:5433/ken_cms_sim?database=ken_interlink"),
    "name_prefix_only_inside": _url("127.0.0.1:5433/x_ken_cms_sim"),
    "uppercase_name": _url("127.0.0.1:5433/KEN_CMS_SIM"),
    "quote_in_name": _url('127.0.0.1:5433/ken_cms_sim"x'),
    "no_database": _url("127.0.0.1:5433"),
    "psycopg_driver": _url("127.0.0.1:5433/ken_cms_sim", driver="postgresql+psycopg"),
    "plain_postgresql": _url("127.0.0.1:5433/ken_cms_sim", driver="postgresql"),
    "sqlite": "sqlite+aiosqlite:///ken_cms_sim.db",
    "garbage": "not a url at all",
}


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_accepts_a_project_local_disposable_database(name: str) -> None:
    assert assert_safe_sim_url(ACCEPTED[name]) == ACCEPTED[name]


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_refuses_and_never_echoes_the_url(name: str) -> None:
    url = REFUSED[name]
    with pytest.raises(ConfigurationError) as exc:
        assert_safe_sim_url(url)
    seen: BaseException | None = exc.value
    while seen is not None:
        assert PASSWORD not in str(seen)
        assert url not in str(seen)
        seen = seen.__cause__ or seen.__context__


@pytest.mark.parametrize("name", ["native_port", "remote_host", "app_database"])
def test_the_provider_refuses_at_construction(name: str) -> None:
    with pytest.raises(ConfigurationError):
        SimulatedCMSProvider(database_url=REFUSED[name], html_engine=None)
