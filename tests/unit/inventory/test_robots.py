"""RFC 9309 robots.txt parsing and matching (ken_services.inventory.robots)."""

from __future__ import annotations

import pytest

from ken_services.inventory.robots import (
    RobotsPolicy,
    allow_all,
    disallow_all,
    normalize_robots_path,
    parse_robots,
    product_token,
)

UA = "KenInterlinkBot/0.1 (+https://example.invalid/bot)"


def policy(text: str, user_agent: str = UA) -> RobotsPolicy:
    return parse_robots(text).policy_for(user_agent)


def test_empty_file_allows_everything() -> None:
    assert policy("").can_fetch("/anything")


def test_factories() -> None:
    assert allow_all().can_fetch("/x")
    assert not disallow_all().can_fetch("/x")
    assert not disallow_all().can_fetch("/")
    assert disallow_all().can_fetch("/robots.txt")
    assert allow_all().crawl_delay is None


def test_robots_txt_always_allowed() -> None:
    assert policy("User-agent: *\nDisallow: /\n").can_fetch("/robots.txt")


def test_product_token() -> None:
    assert product_token(UA) == "keninterlinkbot"
    assert product_token("Plain") == "plain"
    assert product_token("") == ""


def test_specific_group_beats_star() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: KenInterlinkBot\nDisallow: /private\n"
    p = policy(text)
    assert p.can_fetch("/public")
    assert not p.can_fetch("/private/x")


def test_falls_back_to_star_group() -> None:
    text = "User-agent: OtherBot\nDisallow: /\n\nUser-agent: *\nDisallow: /temp\n"
    p = policy(text)
    assert p.can_fetch("/page")
    assert not p.can_fetch("/temp/a")


def test_no_matching_group_allows_all() -> None:
    assert policy("User-agent: OtherBot\nDisallow: /\n").can_fetch("/page")


def test_ua_matching_is_case_insensitive_and_ignores_version() -> None:
    text = "User-agent: keninterlinkbot/9.9\nDisallow: /x\n"
    assert not policy(text).can_fetch("/x")
    assert not policy(text, "KENINTERLINKBOT").can_fetch("/x")


def test_ua_is_not_prefix_matched() -> None:
    assert policy("User-agent: KenInterlink\nDisallow: /\n").can_fetch("/x")


def test_consecutive_user_agents_share_a_group() -> None:
    text = "User-agent: a\nUser-agent: KenInterlinkBot\nDisallow: /shared\n"
    assert not policy(text).can_fetch("/shared")
    assert not policy(text, "a").can_fetch("/shared")


def test_matching_groups_are_merged() -> None:
    text = (
        "User-agent: KenInterlinkBot\nDisallow: /a\n\n"
        "User-agent: other\nDisallow: /b\n\n"
        "User-agent: keninterlinkbot\nDisallow: /c\n"
    )
    p = policy(text)
    assert not p.can_fetch("/a")
    assert p.can_fetch("/b")
    assert not p.can_fetch("/c")


def test_rules_before_any_user_agent_are_ignored() -> None:
    assert policy("Disallow: /\nUser-agent: *\nDisallow: /x\n").can_fetch("/y")


def test_user_agent_after_rule_starts_new_group() -> None:
    text = "User-agent: *\nDisallow: /a\nUser-agent: KenInterlinkBot\nDisallow: /b\n"
    p = policy(text)
    assert p.can_fetch("/a")
    assert not p.can_fetch("/b")


def test_longest_match_wins() -> None:
    text = "User-agent: *\nDisallow: /shop\nAllow: /shop/public\n"
    p = policy(text)
    assert not p.can_fetch("/shop/cart")
    assert p.can_fetch("/shop/public/item")


def test_longer_disallow_beats_shorter_allow() -> None:
    text = "User-agent: *\nAllow: /a\nDisallow: /a/b\n"
    p = policy(text)
    assert p.can_fetch("/a/c")
    assert not p.can_fetch("/a/b/c")


def test_allow_wins_on_equal_length() -> None:
    text = "User-agent: *\nDisallow: /page\nAllow: /page\n"
    assert policy(text).can_fetch("/page")


def test_empty_disallow_allows() -> None:
    assert policy("User-agent: *\nDisallow:\n").can_fetch("/x")


def test_wildcard() -> None:
    text = "User-agent: *\nDisallow: /*.pdf\nDisallow: /*/private/\n"
    p = policy(text)
    assert not p.can_fetch("/docs/a.pdf")
    assert not p.can_fetch("/docs/a.pdf?download=1")
    assert not p.can_fetch("/x/private/y")
    assert p.can_fetch("/docs/a.html")


def test_end_anchor() -> None:
    text = "User-agent: *\nDisallow: /*.pdf$\nDisallow: /exact$\n"
    p = policy(text)
    assert not p.can_fetch("/a.pdf")
    assert p.can_fetch("/a.pdf?x=1")
    assert p.can_fetch("/a.pdfx")
    assert not p.can_fetch("/exact")
    assert p.can_fetch("/exact/more")


def test_query_is_matched() -> None:
    text = "User-agent: *\nDisallow: /*?sort=\n"
    p = policy(text)
    assert not p.can_fetch("/list?sort=asc")
    assert p.can_fetch("/list?page=2")


def test_many_wildcards_do_not_backtrack_catastrophically() -> None:
    text = "User-agent: *\nDisallow: /" + "*a" * 40 + "b$\n"
    assert policy(text).can_fetch("/" + "a" * 5000)


def test_percent_encoding_normalised_both_sides() -> None:
    p = policy("User-agent: *\nDisallow: /%7euser\nDisallow: /caf%c3%a9\n")
    assert not p.can_fetch("/~user/page")
    assert not p.can_fetch("/%7Euser/page")
    assert not p.can_fetch("/café")
    assert not p.can_fetch("/caf%C3%A9")
    assert normalize_robots_path("/a%2fb") == "/a%2Fb"
    assert normalize_robots_path("/a b") == "/a%20b"


def test_pattern_without_leading_slash_is_read_leniently() -> None:
    assert not policy("User-agent: *\nDisallow: private\n").can_fetch("/private/x")


def test_field_names_case_insensitive_and_comments_ignored() -> None:
    text = "USER-AGENT: *   # everyone\nDISALLOW: /x # no x\nallow : /x/ok\n"
    p = policy(text)
    assert not p.can_fetch("/x/no")
    assert p.can_fetch("/x/ok")


def test_bom_and_garbage_tolerated() -> None:
    text = (
        "﻿User-agent: *\n"
        "this is not a rule\n"
        ":::\n"
        "Sitemap: https://example.invalid/sitemap.xml\n"
        "Disallow: /blocked\n"
        "\x00\x01 binary junk\n"
    )
    p = policy(text)
    assert not p.can_fetch("/blocked")
    assert p.can_fetch("/open")


def test_crlf_line_endings() -> None:
    assert not policy("User-agent: *\r\nDisallow: /a\r\n").can_fetch("/a")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5", 5.0), ("0.5", 0.5), ("0", 0.0), ("-1", None), ("abc", None), ("nan", None)],
)
def test_crawl_delay(value: str, expected: float | None) -> None:
    p = policy(f"User-agent: *\nCrawl-delay: {value}\nDisallow: /x\n")
    assert p.crawl_delay == expected


def test_crawl_delay_per_group_and_merged_max() -> None:
    text = (
        "User-agent: *\nCrawl-delay: 30\n\n"
        "User-agent: KenInterlinkBot\nCrawl-delay: 2\n\n"
        "User-agent: KenInterlinkBot\nCrawl-delay: 4\n"
    )
    assert policy(text).crawl_delay == 4.0
    assert policy(text, "Other").crawl_delay == 30.0


def test_empty_path_means_root() -> None:
    assert not policy("User-agent: *\nDisallow: /\n").can_fetch("")
