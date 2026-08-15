"""URL selection, request eligibility, and cache identity helpers."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from typing import Mapping

from image_proxy.config import MatchingConfig


class UrlMatcher:
    """Match requests against configured domain globs and URL regexes."""

    def __init__(self, config: MatchingConfig) -> None:
        self._domains = config.domains
        self._url_patterns = tuple(re.compile(pattern) for pattern in config.url_regex)

    def matches(self, host: str, url: str) -> bool:
        """Return whether *host* and *url* satisfy their configured rules."""
        normalized_host = host.lower()
        domain_matches = any(
            fnmatch.fnmatchcase(normalized_host, pattern.lower())
            for pattern in self._domains
        )
        return domain_matches and (
            not self._url_patterns
            or any(pattern.search(url) for pattern in self._url_patterns)
        )


def is_eligible_request(method: str, headers: Mapping[str, str]) -> bool:
    """Return whether a request can be fetched and transformed."""
    if method.upper() != "GET":
        return False
    return not any(name.lower() == "range" for name in headers)


def build_cache_key(
    url: str, processor_fingerprint: str, headers: Mapping[str, str]
) -> str:
    """Build a deterministic cache identity without retaining private headers."""
    normalized = {key.lower(): value for key, value in headers.items()}
    variants = []
    for name in ("accept", "authorization", "cookie"):
        digest = hashlib.sha256(normalized.get(name, "").encode()).hexdigest()
        variants.append(f"{name}:{digest}")
    payload = "\n".join((url, processor_fingerprint, *variants))
    return hashlib.sha256(payload.encode()).hexdigest()
