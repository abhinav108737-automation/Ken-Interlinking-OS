"""Phase 7 analytics settings (ADR-0015 s5 as amended by s12.3).

* Google egress is OFF by default, and enabling it needs a token file;
* a Google base URL is the exact https host (``analyticsdata.googleapis.com`` /
  ``www.googleapis.com``), or a loopback test fixture in local/ci while egress is disabled;
* the token file path never reaches ``public_settings`` (``GET /meta``);
* the collector binds loopback and accepts loopback origins unless allow_remote (ASK);
* origins are exact (``scheme://host[:port]``), ``null`` and paths refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ken_core.config import REPO_ROOT, parse_origin
from tests.conftest import make_settings

TOKEN_FILE = "/run/secrets/ken-google-token"


def test_defaults_are_off_and_on_the_real_hosts() -> None:
    settings = make_settings()
    assert settings.analytics_google_enabled is False
    assert settings.analytics_google_token_file is None
    assert settings.analytics_ga4_base_url == "https://analyticsdata.googleapis.com"
    assert settings.analytics_gsc_base_url == "https://www.googleapis.com"
    assert settings.collector_host == "127.0.0.1"
    assert settings.collector_allow_remote is False
    assert settings.collector_origins() == ()
    assert Path(settings.analytics_policy_path) == (
        REPO_ROOT / "policies" / "analytics" / "analytics.v1.yaml"
    )
    assert settings.ga4_timezone == "UTC"


def test_a_relative_policy_path_resolves_against_the_repo() -> None:
    settings = make_settings(analytics_policy_path="policies/analytics/analytics.v1.yaml")
    assert Path(settings.analytics_policy_path).is_absolute()


def test_enabling_google_needs_a_token_file() -> None:
    with pytest.raises(ValidationError, match="TOKEN_FILE"):
        make_settings(analytics_google_enabled=True)
    settings = make_settings(analytics_google_enabled=True, analytics_google_token_file=TOKEN_FILE)
    assert settings.analytics_google_enabled is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("analytics_ga4_base_url", "http://analyticsdata.googleapis.com"),
        ("analytics_ga4_base_url", "https://analyticsdata.googleapis.com.evil.test"),
        ("analytics_ga4_base_url", "https://www.googleapis.com"),
        ("analytics_ga4_base_url", "https://analyticsdata.googleapis.com:8443"),
        ("analytics_ga4_base_url", "https://user:pw@analyticsdata.googleapis.com"),
        ("analytics_ga4_base_url", "https://analyticsdata.googleapis.com/v1beta"),
        ("analytics_ga4_base_url", "https://analyticsdata.googleapis.com/?x=1"),
        ("analytics_gsc_base_url", "https://analyticsdata.googleapis.com"),
        ("analytics_gsc_base_url", "https://evil.test"),
        ("analytics_gsc_base_url", "ftp://www.googleapis.com"),
    ],
)
def test_a_google_base_url_is_the_exact_https_host(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


@pytest.mark.parametrize("field", ["analytics_ga4_base_url", "analytics_gsc_base_url"])
def test_a_loopback_fixture_is_local_or_ci_only_and_never_with_egress(field: str) -> None:
    fixture = "http://127.0.0.1:50999"
    for environment in ("local", "ci"):
        assert getattr(make_settings(environment=environment, **{field: fixture}), field) == fixture
    with pytest.raises(ValidationError, match="local or ci"):
        make_settings(environment="staging", database_url=_REAL_DB, **{field: fixture})
    with pytest.raises(ValidationError, match="loopback fixture"):
        make_settings(
            analytics_google_enabled=True,
            analytics_google_token_file=TOKEN_FILE,
            **{field: fixture},
        )


_REAL_DB = "postgresql+asyncpg://app:not-the-dev-password@db.internal:5432/ken"


def test_the_token_file_never_reaches_public_settings() -> None:
    public = make_settings(
        analytics_google_enabled=True,
        analytics_google_token_file=TOKEN_FILE,
        ga4_property_id="123456789",
        gsc_site_url="sc-domain:kenresearch.com",
    ).public_settings()
    text = repr(public)
    assert TOKEN_FILE not in text
    assert "token" not in text.lower()
    assert public["analytics_google_enabled"] is True
    assert public["collector_allow_remote"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ga4_property_id", "properties/123"),
        ("ga4_property_id", "12a"),
        ("ga4_timezone", "Mars/Olympus_Mons"),
        ("gsc_site_url", "sc-domain:"),
        ("gsc_site_url", "sc-domain:Bad Host"),
        ("gsc_site_url", "kenresearch.com"),
        ("gsc_site_url", "https://kenresearch.com/?x=1"),
    ],
)
def test_google_property_settings_are_validated(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_valid_google_property_settings() -> None:
    settings = make_settings(
        ga4_property_id="123456789",
        ga4_timezone="Asia/Kolkata",
        gsc_site_url="https://www.kenresearch.com/",
    )
    assert settings.ga4_timezone == "Asia/Kolkata"
    assert make_settings(gsc_site_url="sc-domain:kenresearch.com").gsc_site_url


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "localhost", "example.com"])  # noqa: S104
def test_the_collector_binds_a_loopback_ip_literal(host: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(collector_host=host)


def test_a_remote_collector_bind_needs_allow_remote() -> None:
    assert make_settings(collector_host="::1").collector_host == "::1"
    assert make_settings(collector_host="10.0.0.5", collector_allow_remote=True).collector_host


@pytest.mark.parametrize(
    "origins",
    ["null", "http://127.0.0.1:9/path", "http://127.0.0.1:9?x", "file:///x", "127.0.0.1:9"],
)
def test_collector_origins_are_exact(origins: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(collector_allowed_origins=origins)


def test_a_non_loopback_origin_needs_allow_remote() -> None:
    with pytest.raises(ValidationError, match="ALLOW_REMOTE"):
        make_settings(collector_allowed_origins="https://www.kenresearch.com")
    settings = make_settings(
        collector_allowed_origins="https://WWW.KenResearch.com, http://localhost:3000/",
        collector_allow_remote=True,
    )
    assert settings.collector_origins() == (
        "https://www.kenresearch.com",
        "http://localhost:3000",
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("HTTP://LOCALHOST/", "http://localhost"),
        ("http://[::1]:9", "http://[::1]:9"),
    ],
)
def test_parse_origin(value: str, expected: str) -> None:
    assert parse_origin(value) == expected
