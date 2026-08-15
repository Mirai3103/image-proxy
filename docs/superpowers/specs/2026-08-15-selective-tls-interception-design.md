# Selective TLS Interception Design

## Goal

Only decrypt HTTPS connections for hosts selected by `matching.domains`.
Within those hosts, only transform requests selected by `matching.url_regex`.
All other HTTPS hosts must pass through mitmproxy as untouched tunnels.

## Configuration Semantics

`matching.domains` is the TLS interception allowlist and must contain at least
one hostname glob. `matching.url_regex` remains a list of regular expressions
searched against the full URL after TLS interception.

Matching changes from OR to AND-between-groups:

- the request host must match at least one `matching.domains` glob; and
- when `matching.url_regex` is non-empty, the URL must match at least one
  configured regex;
- an empty `matching.url_regex` selects every eligible image on an allowed
  host.

The active configuration is:

```yaml
matching:
  domains:
    - "zs.wtcdn.xyz"
  url_regex:
    - "chapter.*\\.webp$"
```

It selects URLs such as
`https://zs.wtcdn.xyz/aprtw/dcn/title/chapter-73/1.webp` and rejects covers,
JPEGs, other hosts, and URLs with characters after `.webp`.

## Runtime Design

The native runner converts every hostname glob into an anchored mitmproxy
`allow_hosts` regular expression matching `host:port`, then supplies those
expressions when starting `mitmdump`. This makes mitmproxy tunnel non-allowed
HTTPS hosts without decrypting them. The addon still applies `UrlMatcher`
after decryption to decide whether an allowed-host response is transformed.

Changing the allowlist requires restarting the proxy, consistent with the
existing configuration workflow.

Plain HTTP cannot be excluded at the connection level in mitmproxy regular
proxy mode, but unmatched HTTP requests continue to pass through unchanged.

## Validation and Failure Behavior

Configuration loading rejects an empty `matching.domains` list because a URL
path is unavailable before an HTTPS connection is decrypted. Domain globs are
converted without evaluating them as user-supplied regular expressions.
Invalid URL regular expressions remain configuration errors.

If an allowed host does not satisfy any configured URL regex, the addon logs
`BYPASS` and leaves its response unchanged. Existing fail-open processing and
cache behavior remains unchanged.

## Tests

Automated tests cover:

- required non-empty domains;
- domain AND URL-regex matching;
- empty URL-regex behavior within an allowed domain;
- conversion of exact and wildcard domains into anchored `allow_hosts`
  arguments;
- runner command construction;
- existing unit and live HTTP/HTTPS smoke suites.

Manual verification confirms that `zs.wtcdn.xyz` chapter WebP files are
processed while unrelated HTTPS hosts are tunneled without mitmproxy-issued
certificates.
