# Selective TLS Interception Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decrypt HTTPS only for configured domain globs, then transform only URLs selected by the configured regular expressions.

**Architecture:** Keep domain and URL matching in `UrlMatcher`, but change its group relationship from OR to AND. Convert validated domain globs into anchored `host:port` regular expressions in the native runner and pass each one to mitmdump through `--allow-hosts`, letting mitmproxy tunnel all other TLS connections without issuing certificates.

**Tech Stack:** Python 3.10, mitmproxy 11.0.2, pytest 8.3.4, PyYAML 6.0.3, curl-based live smoke tests.

## Global Constraints

- `matching.domains` must contain at least one non-empty hostname glob.
- Domain globs are data, never user-supplied regular expressions.
- Every generated `allow_hosts` expression is anchored and matches `host:port`.
- URL regexes are searched against the full URL only after the domain matches.
- An empty `matching.url_regex` selects every eligible image on an allowed domain.
- Non-allowed HTTPS hosts remain untouched TLS tunnels.
- Existing fail-open processing, caching, HTTP pass-through, and restart-based configuration behavior remain unchanged.

---

### Task 1: Enforce Domain AND URL Matching

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_matcher.py`
- Modify: `src/image_proxy/config.py`
- Modify: `src/image_proxy/matcher.py`

**Interfaces:**
- Consumes: `MatchingConfig(domains: tuple[str, ...], url_regex: tuple[str, ...])`
- Produces: `UrlMatcher.matches(host: str, url: str) -> bool` with AND-between-groups semantics.

- [ ] **Step 1: Write failing configuration and matcher tests**

```python
def test_load_config_rejects_empty_matching_domains(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID.replace('domains: ["*.cdn.test"]', "domains: []"))
    with pytest.raises(ConfigError, match="matching.domains must contain"):
        load_config(path)


def test_matcher_requires_domain_and_full_url_regex() -> None:
    matcher = UrlMatcher(MatchingConfig(("*.cdn.test",), (r"chapter.*\.webp$",)))
    assert matcher.matches("img.cdn.test", "https://img.cdn.test/chapter-73/1.webp")
    assert not matcher.matches("img.cdn.test", "https://img.cdn.test/cover.webp")
    assert not matcher.matches("other.test", "https://other.test/chapter-73/1.webp")
    assert not matcher.matches("img.cdn.test", "https://img.cdn.test/chapter-73/1.webp?x=1")


def test_empty_url_regex_matches_every_url_on_allowed_domain() -> None:
    matcher = UrlMatcher(MatchingConfig(("*.cdn.test",), ()))
    assert matcher.matches("img.cdn.test", "https://img.cdn.test/cover.webp")
    assert not matcher.matches("other.test", "https://other.test/cover.webp")
```

- [ ] **Step 2: Run focused tests and confirm failures are caused by the old behavior**

Run: `uv run pytest tests/test_config.py tests/test_matcher.py -q`

Expected: the empty-domain test does not raise, while the matcher assertions expose OR semantics and empty-regex behavior.

- [ ] **Step 3: Implement minimal validation and AND matching**

```python
if not domains:
    raise ConfigError("matching.domains must contain at least one hostname glob")
```

```python
domain_matches = any(
    fnmatch.fnmatchcase(normalized_host, pattern.lower())
    for pattern in self._domains
)
return domain_matches and (
    not self._url_patterns or any(pattern.search(url) for pattern in self._url_patterns)
)
```

- [ ] **Step 4: Run focused tests until green**

Run: `uv run pytest tests/test_config.py tests/test_matcher.py -q`

Expected: all focused tests pass with no warnings or errors.

- [ ] **Step 5: Commit the behavior and implementation plan**

```bash
git add docs/superpowers/plans/2026-08-15-selective-tls-interception.md tests/test_config.py tests/test_matcher.py src/image_proxy/config.py src/image_proxy/matcher.py
git commit -m "feat: require domain and URL matches"
```

### Task 2: Generate Native mitmproxy Allowlist Arguments

**Files:**
- Modify: `tests/test_runner.py`
- Modify: `src/image_proxy/runner.py`

**Interfaces:**
- Produces: `domain_glob_to_allow_hosts_regex(domain_glob: str) -> str`.
- Produces: `build_mitmdump_command(...) -> list[str]` containing one `--allow-hosts REGEX` pair per configured domain.

- [ ] **Step 1: Write failing conversion and command tests**

```python
def test_domain_globs_become_anchored_host_port_regexes() -> None:
    exact = domain_glob_to_allow_hosts_regex("zs.wtcdn.xyz")
    wildcard = domain_glob_to_allow_hosts_regex("*.cdn.test")
    assert re.search(exact, "zs.wtcdn.xyz:443")
    assert not re.search(exact, "evil-zs.wtcdn.xyz:443")
    assert not re.search(exact, "zs.wtcdn.xyz.evil:443")
    assert re.search(wildcard, "img.cdn.test:8443")
    assert not re.search(wildcard, "img.cdn.test.evil:8443")
```

Update the command expectation to include:

```python
"--allow-hosts",
domain_glob_to_allow_hosts_regex("*.example-cdn.com"),
```

- [ ] **Step 2: Run the runner tests and confirm missing API/arguments fail**

Run: `uv run pytest tests/test_runner.py -q`

Expected: collection or assertions fail because the converter and allowlist arguments do not exist.

- [ ] **Step 3: Implement safe glob conversion and command assembly**

```python
def domain_glob_to_allow_hosts_regex(domain_glob: str) -> str:
    translated = fnmatch.translate(domain_glob)
    assert translated.endswith(r"\Z")
    return rf"\A{translated[:-2]}:\d+\Z"
```

Build `allow_hosts_args` from `config.matching.domains` and splice it into the mitmdump command before the addon script arguments.

- [ ] **Step 4: Run runner tests until green**

Run: `uv run pytest tests/test_runner.py -q`

Expected: all runner tests pass.

- [ ] **Step 5: Commit runner support**

```bash
git add tests/test_runner.py src/image_proxy/runner.py
git commit -m "feat: restrict TLS interception by host"
```

### Task 3: Update User-Facing Configuration and Documentation

**Files:**
- Modify: `config.example.yaml`
- Modify locally only: `config.yaml`
- Modify: `README.md`

**Interfaces:**
- `config.yaml` active matching values: `domains: ["zs.wtcdn.xyz"]`, `url_regex: ["chapter.*\\.webp$"]`.

- [ ] **Step 1: Update configuration examples and matching documentation**

Change the example comments and README from OR semantics to: domain allowlist first, then optional URL-regex filtering. Explain that unrelated HTTPS hosts are tunneled without mitmproxy certificates and that allowlist changes require restart.

- [ ] **Step 2: Update the local active configuration**

```yaml
matching:
  domains:
    - "zs.wtcdn.xyz"
  url_regex:
    - "chapter.*\\.webp$"
```

Keep `config.yaml` untracked.

- [ ] **Step 3: Validate both YAML files through the real loader**

Run: `uv run python -c 'from pathlib import Path; from image_proxy.config import load_config; [load_config(Path(p)) for p in ("config.example.yaml", "config.yaml")]'`

Expected: exit status 0.

- [ ] **Step 4: Commit tracked documentation changes**

```bash
git add config.example.yaml README.md
git commit -m "docs: describe selective TLS interception"
```

### Task 4: Prove Allowed Interception and Unrelated TLS Tunneling

**Files:**
- Modify: `tests/smoke/test_live_proxy.py`

**Interfaces:**
- Consumes: `build_mitmdump_command(...)` so the smoke suite exercises the production launcher command.
- Verifies: `localhost` is intercepted and processed; `127.0.0.1` reaches the same TLS origin using its original certificate and original bytes.

- [ ] **Step 1: Change the smoke fixture to use an allowed hostname and add the tunnel assertion**

Configure `matching.domains` as `["localhost"]` and use `localhost` for the processed HTTP/HTTPS request. In the HTTPS case, fetch the same origin as `127.0.0.1` with `--cacert origin.crt`, save it separately, and assert its bytes equal the source image. This request can succeed with the origin certificate only when mitmproxy leaves the TLS connection untouched.

- [ ] **Step 2: Launch mitmdump using the production command builder**

Load the temporary config and call `build_mitmdump_command`, while retaining smoke-only `confdir` and `ssl_insecure=true` arguments.

- [ ] **Step 3: Run the smoke suite and fix only behavior required by the approved spec**

Run: `uv run pytest -m smoke tests/smoke/test_live_proxy.py -q`

Expected: both HTTP and HTTPS cases pass; HTTPS proves the allowed request is transformed/cached and the unrelated hostname is a byte-for-byte origin-certified tunnel.

- [ ] **Step 4: Run the full verification suite**

Run: `uv run pytest -q`

Run: `uv run pytest -m smoke tests/smoke/test_live_proxy.py -q`

Expected: all unit and smoke tests pass with no failures.

- [ ] **Step 5: Commit the smoke coverage**

```bash
git add tests/smoke/test_live_proxy.py
git commit -m "test: verify unrelated TLS hosts remain tunneled"
```
