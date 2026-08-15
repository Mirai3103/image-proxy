from image_proxy.config import MatchingConfig
from image_proxy.matcher import UrlMatcher, build_cache_key, is_eligible_request


def test_matcher_requires_domain_and_full_url_regex() -> None:
    matcher = UrlMatcher(MatchingConfig(("*.cdn.test",), (r"chapter.*\.webp$",)))

    assert matcher.matches(
        "img.cdn.test", "https://img.cdn.test/chapter-73/1.webp"
    )
    assert not matcher.matches(
        "img.cdn.test", "https://img.cdn.test/cover.webp"
    )
    assert not matcher.matches(
        "other.test", "https://other.test/chapter-73/1.webp"
    )
    assert not matcher.matches(
        "img.cdn.test", "https://img.cdn.test/chapter-73/1.webp?x=1"
    )


def test_empty_url_regex_matches_every_url_on_allowed_domain() -> None:
    matcher = UrlMatcher(MatchingConfig(("*.cdn.test",), ()))

    assert matcher.matches("img.cdn.test", "https://img.cdn.test/cover.webp")
    assert not matcher.matches("other.test", "https://other.test/cover.webp")


def test_empty_rules_match_nothing() -> None:
    assert not UrlMatcher(MatchingConfig((), ())).matches(
        "cdn.test", "https://cdn.test/page.jpg"
    )


def test_request_eligibility_requires_get_without_range() -> None:
    assert is_eligible_request("GET", {})
    assert not is_eligible_request("POST", {})
    assert not is_eligible_request("GET", {"Range": "bytes=0-99"})


def test_cache_key_varies_by_private_headers_without_exposing_them() -> None:
    first = build_cache_key(
        "https://cdn.test/page.webp", "processor-v1", {"Cookie": "secret-a"}
    )
    second = build_cache_key(
        "https://cdn.test/page.webp", "processor-v1", {"Cookie": "secret-b"}
    )
    assert first != second
    assert "secret" not in first
    assert len(first) == 64


def test_matching_is_case_insensitive_for_hosts_and_headers() -> None:
    matcher = UrlMatcher(MatchingConfig(("*.CDN.TEST",), ()))

    assert matcher.matches("IMG.cdn.test", "https://IMG.cdn.test/page.webp")
    assert is_eligible_request("get", {"rAnGe": "bytes=0-99"}) is False


def test_cache_key_normalizes_private_header_names() -> None:
    lower = build_cache_key(
        "https://cdn.test/page.webp",
        "processor-v1",
        {"accept": "image/webp", "authorization": "Bearer token", "cookie": "a=b"},
    )
    mixed = build_cache_key(
        "https://cdn.test/page.webp",
        "processor-v1",
        {
            "Accept": "image/webp",
            "Authorization": "Bearer token",
            "Cookie": "a=b",
        },
    )

    assert lower == mixed
