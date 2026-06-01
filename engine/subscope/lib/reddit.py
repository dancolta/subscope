"""Reddit fetcher: RSS/Atom only (keyless, accountless).

Public surface (what callers use):
  - fetch_delta(sub, last_seen_id, max_limit)   : daily-delta scan
  - fetch_post(url_or_id)                       : single submission via /comments/<id>/.rss
  - fetch_user_about(username)                  : OP profile vetting (now None)
  - fetch_user_recent_subs(username, limit)     : OP audience-fit histogram
  - canonical_url(post_data)                    : URL normalization
  - parse_post(child)                           : normalize a JSON listing entry
  - parse_atom_entry(entry)                     : normalize an Atom <entry>
  - fetch_json(url)                             : raw public GET with retry
  - fetch_xml(url)                              : single-URL RSS/Atom GET with retry
  - fetch_xml_resilient(path)                   : dual-host RSS GET (www, then old.reddit)
  - _safe_username(username)                    : path-injection guard

Why RSS, not JSON: as of 2026-05-29 Reddit's edge (Fastly/Varnish) returns an
instant `403 Blocked` (Retry-After: 0) for every unauthenticated `.json`
endpoint, www and old reddit alike, regardless of User-Agent. The machine IP is
fine (HTML + RSS both return 200). The RSS/Atom surface
(`/r/<sub>/new/.rss`, `/user/<x>/comments/.rss`) still returns 200 with no
credentials, so the fetcher reads those instead.

Header discipline (2026-06-01): RSS GETs send a User-Agent and NOTHING ELSE. The
edge began 403ing any keyless RSS request that carries an explicit `Accept`
header from a non-browser TLS client (any value, including `*/*`); the identical
URL with UA-only returns 200. The trigger is the header's presence, not its
value, so the fetcher omits Accept entirely. This is a deterministic fingerprint
block, not a transient: a request with Accept fails on every retry. See
_fetch_xml_attempt and test_reddit_no_accept_header. For resilience the fetcher
fails over across two RSS hosts (www.reddit.com, then old.reddit.com) on a
403/5xx/network error, but never on a 429 (the rate-limit bucket is per-IP and
shared across hosts). See fetch_xml_resilient + RSS_HOSTS. No HTML scrape, no
auth host, ever.

OAuth was removed in v0.2 and stays removed. The product positions on
"Free, no API keys"; reintroducing OAuth is explicitly out of scope.

RSS does NOT carry score, num_comments, upvote_ratio, or locked state. Those
fields default (score=0, num_comments=0, upvote_ratio=None, locked=False) and
the scorer degrades gracefully (see score.py). `/user/<x>/about.json` (karma +
age) is also 403, so fetch_user_about returns None and author_vet fails open.
"""
from __future__ import annotations

import html
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()


# ─── Constants ────────────────────────────────────────────────────────

USER_AGENT = "subscope/0.1 (research tool, github.com/dancolta)"
MAX_RETRIES = 3
BASE_BACKOFF = 2.0

# Rate-limit discipline. Reddit's keyless RSS surface enforces a per-IP token
# bucket (~100 req / 10 min, surfaced via x-ratelimit-* headers). A daily manual
# run fires ~18 sub feeds plus an author comments feed per plausible OP, which
# will trip the limit if we burst. We pace requests instead.
#
# MIN_REQUEST_INTERVAL: minimum wall-clock gap between ANY two Reddit GETs. A
#   once or twice daily run can afford ~30 to 60s of total spacing.
# RATELIMIT_REMAINING_FLOOR: when a response says this few tokens remain, pause
#   until the bucket resets rather than bursting into a 429.
# MAX_RATELIMIT_PAUSE: hard cap on any single ratelimit/Retry-After sleep, so a
#   bogus or huge reset value can never hang a run.
MIN_REQUEST_INTERVAL = 1.6
RATELIMIT_REMAINING_FLOOR = 2.0
MAX_RATELIMIT_PAUSE = 60.0

# Reddit search `q` safe cap. Longer queries are truncated at a word boundary
# before URL-encoding (search.rss rejects over-long queries). Allowlists keep
# caller-supplied sort/timeframe values off the URL unless they are valid.
QUERY_MAX_LEN = 512
_SEARCH_SORTS = ("new", "top", "relevance", "hot", "comments")
_SEARCH_TIMEFRAMES = ("hour", "day", "week", "month", "year", "all")

# Atom namespace used by Reddit RSS feeds.
_ATOM_NS = "{http://www.w3.org/2005/Atom}"

# Reddit usernames are A-Za-z0-9_- with a 3-20 practical range. We accept up to
# 32 to be safe. Anything outside this pattern is rejected before URL building
# to defuse path-segment injection on reddit.com endpoints.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _log(msg: str) -> None:
    sys.stderr.write(f"[reddit] {msg}\n")
    sys.stderr.flush()


# ─── Fetch reachability stats ─────────────────────────────────────────
# Lets a caller (cli.fetch_score) tell three run states apart:
#   ok           : at least one feed fetched, no rate-limit hit -> normal run
#   rate_limited : one or more GETs hit 429 (retry shortly), possibly partial
#   blocked      : every GET failed for a NON-429 reason (true edge block / net)
#
# Counters (per run, reset via reset_fetch_stats):
#   ok           : feed GETs that returned parseable XML
#   failed       : feed GETs that failed for a non-429 reason (403/404/net/parse)
#   rate_limited : feed GETs that ultimately returned None due to a 429
# `drained` flips True once a response reports the token bucket at/under the
#   floor (or a 429 lands), so the caller can stop bursting mid-run.

_FETCH_STATS = {"ok": 0, "failed": 0, "rate_limited": 0, "fallback_used": 0}
_RATE_STATE = {"drained": False}

# Ordered RSS hosts for the dual-host 403 failover (SS-104). Both return 200 for
# keyless RSS today; serving the same Atom shape, so one parser covers both. www
# is primary; old.reddit is the failover when www returns 403/5xx/network (NOT on
# 429, which is a per-IP bucket shared across hosts). No HTML scrape, no auth host
# ever (see test_no_oauth_surface). This is the "keyless 403 workaround".
RSS_HOSTS = ("www.reddit.com", "old.reddit.com")

# Wall-clock timestamp of the last Reddit GET, for proactive inter-request
# spacing. Module-level so spacing holds across subs AND author-vet calls.
_last_request_at = 0.0


def reset_fetch_stats() -> None:
    """Zero the per-run feed counters and rate state. Call before a fetch batch."""
    _FETCH_STATS["ok"] = 0
    _FETCH_STATS["failed"] = 0
    _FETCH_STATS["rate_limited"] = 0
    _FETCH_STATS["fallback_used"] = 0
    _RATE_STATE["drained"] = False


def get_fetch_stats() -> dict[str, int]:
    """Return a copy of the per-run feed counters."""
    return dict(_FETCH_STATS)


def is_rate_limited() -> bool:
    """True once a 429 landed or the token bucket reported at/under the floor.

    The CLI checks this between subs to stop bursting and return partial
    results instead of draining the bucket and wholesale-failing the run.
    """
    return _RATE_STATE["drained"]


def _sleep(seconds: float) -> None:
    """Indirection over time.sleep so tests can assert spacing without waiting."""
    if seconds > 0:
        time.sleep(seconds)


def _throttle() -> None:
    """Proactively space Reddit GETs at least MIN_REQUEST_INTERVAL apart."""
    global _last_request_at
    now = time.monotonic()
    elapsed = now - _last_request_at
    if _last_request_at and elapsed < MIN_REQUEST_INTERVAL:
        _sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def _ratelimit_pause_from_headers(headers: Any) -> None:
    """Read x-ratelimit-remaining / x-ratelimit-reset from a 200 response and
    pause until reset when the bucket is nearly empty, so we never burst into a
    429. Flips the drained flag so the caller can stop early. Capped wait."""
    if headers is None:
        return
    remaining = _header_float(headers, "x-ratelimit-remaining")
    if remaining is None:
        return
    if remaining <= RATELIMIT_REMAINING_FLOOR:
        _RATE_STATE["drained"] = True
        reset = _header_float(headers, "x-ratelimit-reset")
        pause = min(reset, MAX_RATELIMIT_PAUSE) if reset and reset > 0 else MIN_REQUEST_INTERVAL
        _log(f"ratelimit low (remaining={remaining}), pausing {pause:.1f}s until reset")
        _sleep(pause)


def _header_float(headers: Any, name: str) -> float | None:
    """Parse a numeric header value to float, or None if absent/unparseable."""
    if headers is None:
        return None
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ─── Username safety ──────────────────────────────────────────────────

def _safe_username(username: str | None) -> str | None:
    """Return a username safe for URL path interpolation, or None if invalid.

    Strips a leading 'u/' or '/u/' if present. Returns None for empty input,
    None for input that fails the username regex (rejects hostile input like
    'x/about.json?dummy=', '../etc/passwd', etc.).
    """
    if not username:
        return None
    cleaned = username.lstrip("/").removeprefix("u/")
    return cleaned if _USERNAME_RE.match(cleaned) else None


# ─── URL canonicalization ─────────────────────────────────────────────

def canonical_url(reddit_post_data: dict[str, Any]) -> str:
    """Canonicalize a Reddit post URL to https://reddit.com/comments/<t3_id>/.

    Post IDs are globally unique on Reddit; the subreddit slug is unnecessary
    for the canonical form. Strips host variants (old., np., www., m.), query
    strings, and trailing slashes.
    """
    raw_id = str(reddit_post_data.get("id") or reddit_post_data.get("name") or "").strip()
    raw_id = re.sub(r"^t3_", "", raw_id)
    if not raw_id:
        permalink = str(reddit_post_data.get("permalink") or "")
        m = re.search(r"/comments/([a-z0-9]+)", permalink, re.I)
        if not m:
            return ""
        raw_id = m.group(1)
    return f"https://reddit.com/comments/{raw_id}/"


# ─── Raw fetchers (JSON kept for parser-contract tests, XML is the live path) ──

def fetch_json(url: str, timeout: int = 15) -> dict[str, Any] | None:
    """Fetch JSON with retry on 429 and content-type validation.

    Reddit's anonymous JSON surface 403s as of 2026-05-29 and there are NO
    production callers left (the judge skill moved to fetch_post / RSS). Retained
    ONLY so the parse-contract and path-injection-guard tests
    (test_unsafe_username_rejected) keep a stable patch target. Do not call from
    new code; use fetch_post / fetch_xml_resilient.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)

    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if "json" not in content_type and "text/html" in content_type:
                    _log(f"anti-bot HTML response (Content-Type: {content_type})")
                    return None
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                delay = _retry_after_delay(e, attempt)
                _log(f"429 rate-limited, retry {attempt + 1}/{MAX_RETRIES} after {delay:.1f}s")
                if attempt < MAX_RETRIES - 1:
                    _sleep(delay)
                    continue
                _log("429 retries exhausted")
                return None
            elif e.code in (403, 404):
                _log(f"HTTP {e.code}: {url}")
                return None
            else:
                _log(f"HTTP {e.code}: {e.reason}")
                return None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(f"network error: {e}")
            return None
        except json.JSONDecodeError as e:
            _log(f"JSON decode error: {e}")
            return None
    return None


def _fetch_xml_attempt(url: str, timeout: int = 15) -> tuple[str, ET.Element | None]:
    """Single-URL RSS/Atom GET. Returns one of:
      ("ok", root) | ("rate_limited", None) | ("failed", None)

    Does the per-request work (throttle spacing, 429 retry/backoff, x-ratelimit
    header pacing) but does NOT touch the per-sub OUTCOME counters. The CALLER
    decides the outcome, so a www 403 that then succeeds on old.reddit can be
    counted once (as ok) instead of as failed+ok. This is the seam the dual-host
    failover (fetch_xml_resilient) and the single-URL wrapper (fetch_xml) share.
    """
    # User-Agent ONLY. Do NOT send an explicit Accept header: as of 2026-06-01
    # Reddit's edge (Fastly) 403s any keyless RSS GET that carries an Accept
    # header from a non-browser TLS client, www and old alike, regardless of the
    # Accept value. The same URL with UA-only returns 200. This is a fingerprint
    # block, not a transient, so it fails on every retry until the header is
    # dropped. Omitting Accept sends NO Accept header at all (urllib adds no
    # default), and the edge returns 200 for that. Note an explicit Accept: */*
    # is also 403'd, so the trigger is the header's presence, not its value.
    # See test_reddit_no_accept_header.
    headers = {"User-Agent": USER_AGENT}
    req = urllib.request.Request(url, headers=headers)

    for attempt in range(MAX_RETRIES):
        _throttle()  # proactive inter-request spacing, every attempt
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
                body = resp.read().decode("utf-8")
                root = ET.fromstring(body)
                # Header-aware pacing: if the bucket is nearly empty, pause now
                # (until reset, capped) so the NEXT GET does not 429.
                _ratelimit_pause_from_headers(getattr(resp, "headers", None))
                return ("ok", root)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # The bucket is drained. Flag it so the caller stops bursting.
                _RATE_STATE["drained"] = True
                delay = _retry_after_delay(e, attempt)
                _log(f"429 rate-limited, retry {attempt + 1}/{MAX_RETRIES} after {delay:.1f}s")
                if attempt < MAX_RETRIES - 1:
                    _sleep(delay)
                    continue
                _log("429 retries exhausted")
                return ("rate_limited", None)
            elif e.code in (403, 404):
                _log(f"HTTP {e.code}: {url}")
                return ("failed", None)
            else:
                _log(f"HTTP {e.code}: {e.reason}")
                return ("failed", None)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(f"network error: {e}")
            return ("failed", None)
        except ET.ParseError as e:
            _log(f"XML parse error: {e}")
            return ("failed", None)
    return ("failed", None)


def fetch_xml(url: str, timeout: int = 15) -> ET.Element | None:
    """Single-URL RSS/Atom fetch (back-compat primitive). Increments the per-sub
    outcome counters exactly as before: ok / rate_limited / failed.

    New callers that want the dual-host 403 failover should use
    fetch_xml_resilient. This wrapper is kept so existing callers and the
    fetch_xml contract tests behave identically.
    """
    status, root = _fetch_xml_attempt(url, timeout=timeout)
    if status == "ok":
        _FETCH_STATS["ok"] += 1
        return root
    if status == "rate_limited":
        _FETCH_STATS["rate_limited"] += 1
        return None
    _FETCH_STATS["failed"] += 1
    return None


def fetch_xml_resilient(path: str, timeout: int = 15) -> ET.Element | None:
    """Fetch an RSS/Atom path across RSS_HOSTS with 403/5xx/network failover.

    `path` is host-relative and begins with '/', e.g. '/r/saas/new/.rss?limit=25'.
    Per-host precedence (the "100% keyless 403 workaround"):
      - ok            -> count one ok (+ fallback_used if a non-primary host
                         served it) and return the parsed root.
      - rate_limited  -> the per-IP token bucket is drained; it is SHARED across
                         hosts, so do NOT advance. Count one rate_limited, flip
                         drained, return None (caller stops bursting).
      - failed        -> try the next host.
    All hosts failed -> count one failed, return None.

    Exactly ONE outcome counter is incremented per call, so the
    ok + failed + rate_limited == subs-attempted invariant holds with failover.
    """
    for i, host in enumerate(RSS_HOSTS):
        url = f"https://{host}{path}"
        status, root = _fetch_xml_attempt(url, timeout=timeout)
        if status == "ok":
            _FETCH_STATS["ok"] += 1
            if i > 0:
                _FETCH_STATS["fallback_used"] += 1
                _log(f"served via failover host {host}: {path}")
            return root
        if status == "rate_limited":
            _RATE_STATE["drained"] = True
            _FETCH_STATS["rate_limited"] += 1
            return None
        # failed: try the next host
    _FETCH_STATS["failed"] += 1
    return None


def _retry_after_delay(err: urllib.error.HTTPError, attempt: int) -> float:
    """Backoff delay for a 429. Prefer Retry-After, then x-ratelimit-reset, else
    exponential. Always capped at MAX_RATELIMIT_PAUSE so a bogus header value can
    never hang the run."""
    delay = BASE_BACKOFF * (2 ** attempt)
    hdrs = getattr(err, "headers", None)
    retry_after = _header_float(hdrs, "Retry-After")
    if retry_after is not None and retry_after > 0:
        delay = retry_after
    else:
        reset = _header_float(hdrs, "x-ratelimit-reset")
        if reset is not None and reset > 0:
            delay = reset
    return min(delay, MAX_RATELIMIT_PAUSE)


# ─── JSON listing parser (kept for the parse contract + canonical tests) ──

def parse_post(child: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a Reddit JSON listing child into our post shape.

    Returns None on bad data. RSS is the live source now (see parse_atom_entry),
    but this stays as the canonical contract for the post dict shape and is
    still exercised by the canonical-URL test suite.
    """
    if child.get("kind") != "t3":
        return None
    data = child.get("data", {})
    permalink = str(data.get("permalink", "")).strip()
    if not permalink or "/comments/" not in permalink:
        return None

    canon = canonical_url(data)
    if not canon:
        return None

    post_id = re.sub(r"^t3_", "", str(data.get("id", ""))).strip()
    if not post_id:
        return None

    selftext = str(data.get("selftext") or "")
    author = str(data.get("author") or "[deleted]")
    removed = bool(data.get("removed_by_category") or data.get("removed") or
                   author == "[deleted]" or selftext == "[removed]")

    # NSFW: Reddit flags posts via over_18 (post-level) and the host subreddit
    # may also be NSFW. Either one is sufficient to reject.
    over_18 = bool(data.get("over_18") or data.get("thumbnail") == "nsfw")

    # Crosspost detection: posts with crosspost_parent_list are reposts from
    # other subs. Even if the host sub is SFW, the original may not be.
    crosspost_parent = data.get("crosspost_parent_list") or []
    is_crosspost = bool(crosspost_parent)
    if is_crosspost:
        parent = crosspost_parent[0] if isinstance(crosspost_parent, list) and crosspost_parent else {}
        if parent.get("over_18"):
            over_18 = True

    return {
        "id": post_id,
        "subreddit": str(data.get("subreddit", "")).strip(),
        "title": str(data.get("title", "")).strip(),
        "url": f"https://www.reddit.com{permalink}",
        "canonical_url": canon,
        "author": author,
        "created_utc": int(data.get("created_utc") or 0),
        "score": int(data.get("score") or 0),
        "num_comments": int(data.get("num_comments") or 0),
        "body": selftext[:1000],
        "upvote_ratio": data.get("upvote_ratio"),
        "removed": removed,
        "locked": bool(data.get("locked")),
        "over_18": over_18,
        "is_crosspost": is_crosspost,
    }


# ─── Atom entry parser (the live path) ────────────────────────────────

def _atom_text(entry: ET.Element, tag: str) -> str:
    """Return stripped text of the first <tag> child in the Atom namespace, or ''."""
    el = entry.find(f"{_ATOM_NS}{tag}")
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _parse_iso8601_to_epoch(ts: str) -> int:
    """Parse an ISO8601 timestamp (e.g. '2026-05-29T10:14:46+00:00') to epoch int.

    Returns 0 on empty/unparseable input. Handles a trailing 'Z' (UTC) which
    older Python's fromisoformat rejects.
    """
    if not ts:
        return 0
    cleaned = ts.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(cleaned).timestamp())
    except (ValueError, OverflowError):
        return 0


def _clean_atom_body(raw_html: str) -> str:
    """Strip Reddit's RSS content chrome and return a plain-text body, capped 1000.

    Reddit wraps post bodies in `<!-- SC_OFF -->...<!-- SC_ON -->` and appends a
    'submitted by /u/x [link] [comments]' footer. We HTML-unescape, drop the
    footer, strip tags to text, and cap to 1000 chars to match parse_post.
    """
    if not raw_html:
        return ""
    text = html.unescape(raw_html)
    # Drop Reddit's content markers and the trailing "submitted by ... [link]
    # [comments]" chrome. After html.unescape the &#32; separators are spaces,
    # so match the literal "submitted by" through end-of-string.
    text = re.sub(r"<!--\s*SC_O(FF|N)\s*-->", "", text)
    text = re.sub(r"submitted by\b.*$", "", text, flags=re.S | re.I)
    # Strip remaining HTML tags to plain text, collapse whitespace.
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)  # second pass for entities revealed after tag strip
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1000]


def parse_atom_entry(entry: ET.Element) -> dict[str, Any] | None:
    """Normalize a Reddit Atom <entry> into the same dict shape as parse_post.

    Returns None on bad data (missing id, missing permalink). RSS does not carry
    engagement metrics, so score/num_comments default to 0, upvote_ratio to None,
    and locked/removed to False (the feed lists live posts only).

    Output keys (contract, identical to parse_post): id, subreddit, title, url,
    canonical_url, author, created_utc, score, num_comments, body, upvote_ratio,
    removed, locked, over_18, is_crosspost.
    """
    # Permalink: the <link href=...> of the entry.
    permalink = ""
    for link in entry.findall(f"{_ATOM_NS}link"):
        href = link.get("href")
        if href:
            permalink = href.strip()
            break
    if not permalink or "/comments/" not in permalink:
        return None

    # Post id: <id>t3_xxxx</id>, falling back to the permalink.
    raw_id = _atom_text(entry, "id")
    post_id = re.sub(r"^t3_", "", raw_id).strip()
    if not post_id:
        m = re.search(r"/comments/([a-z0-9]+)", permalink, re.I)
        if not m:
            return None
        post_id = m.group(1)
    # Guard: an <id> from a comment feed is t1_ (a comment), not a post. Only
    # keep ids that resolve from the t3 namespace or the post permalink.
    if not re.fullmatch(r"[A-Za-z0-9]+", post_id):
        return None

    canon = canonical_url({"id": post_id, "permalink": permalink})
    if not canon:
        return None

    # Subreddit: <category term="SaaS"> on the entry.
    subreddit = ""
    cat = entry.find(f"{_ATOM_NS}category")
    if cat is not None:
        subreddit = (cat.get("term") or "").strip()

    # Author: <author><name>/u/Name</name></author>.
    author = "[deleted]"
    author_el = entry.find(f"{_ATOM_NS}author/{_ATOM_NS}name")
    if author_el is not None and author_el.text:
        author = author_el.text.strip().lstrip("/").removeprefix("u/") or "[deleted]"

    title = _atom_text(entry, "title")
    published = _atom_text(entry, "published") or _atom_text(entry, "updated")
    created_utc = _parse_iso8601_to_epoch(published)

    content_el = entry.find(f"{_ATOM_NS}content")
    body = _clean_atom_body(content_el.text if content_el is not None else "")

    return {
        "id": post_id,
        "subreddit": subreddit,
        "title": title,
        "url": permalink,
        "canonical_url": canon,
        "author": author,
        "created_utc": created_utc,
        "score": 0,
        "num_comments": 0,
        "body": body,
        "upvote_ratio": None,
        "removed": False,
        "locked": False,
        "over_18": False,
        "is_crosspost": False,
    }


def fetch_feed(url: str, timeout: int = 15) -> list[dict[str, Any]] | None:
    """Fetch any Reddit RSS/Atom feed URL, return normalized posts.

    Returns None when the feed was UNREACHABLE (so callers can distinguish
    unreachable from empty), and [] for a reachable-but-empty feed. Shares the
    global request throttle, x-ratelimit pacing, and 429 backoff via fetch_xml,
    so discovery search calls draw from the same per-IP rate-limit budget as the
    daily scan. Used by discover.py for keyless search.rss / search-within-sub.

    Reddit-host URLs go through the dual-host failover (fetch_xml_resilient);
    any other host falls back to the single-URL fetch_xml.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.netloc.endswith("reddit.com"):
        rel = parts.path + (f"?{parts.query}" if parts.query else "")
        root = fetch_xml_resilient(rel, timeout=timeout)
    else:
        root = fetch_xml(url, timeout=timeout)
    if root is None:
        return None
    posts: list[dict[str, Any]] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        p = parse_atom_entry(entry)
        if p:
            posts.append(p)
    return posts


def _normalize_sub(sub: str) -> str:
    """Strip an optional 'r/' or '/r/' prefix and surrounding slashes/space.

    NOT lstrip('r/'): that strips every leading 'r' and '/' as a character class,
    mangling subs that start with 'r' (rails -> ails, recruiting -> ecruiting).
    Callers URL-encode the result with quote(safe='') so '/' and '.' cannot escape
    the path segment.
    """
    s = (sub or "").strip()
    if s.startswith("/r/"):
        s = s[3:]
    elif s.startswith("r/"):
        s = s[2:]
    return s.strip("/").strip()


def fetch_search(
    sub: str | None,
    query: str,
    *,
    sort: str = "new",
    t: str | None = None,
    limit: int = 25,
    restrict_sr: bool = True,
    timeout: int = 12,
) -> list[dict[str, Any]] | None:
    """Keyless Reddit search via search.rss. Within-sub when `sub` is set and
    restrict_sr is True, global search otherwise.

    Returns normalized posts (the same parse_atom_entry shape fetch_feed emits),
    [] for a reachable-but-empty feed, and None when the feed is UNREACHABLE, so
    callers can tell a quiet sub apart from a host failure. Delegates to
    fetch_feed, so it inherits the dual-host 403 failover, the per-IP request
    throttle, x-ratelimit pacing, and 429 backoff. The query is truncated to
    QUERY_MAX_LEN at a word boundary, then URL-encoded. `t` is sent only with
    sort in (top, relevance); Reddit ignores it for sort=new (filter by
    created_utc client-side instead).

    Keyless by design: the URL host is always an RSS host, never an auth host.
    """
    q = (query or "").strip()
    if not q:
        return []
    if len(q) > QUERY_MAX_LEN:
        q = q[:QUERY_MAX_LEN].rsplit(" ", 1)[0]
    if sort not in _SEARCH_SORTS:
        sort = "new"

    params = [f"q={urllib.parse.quote(q)}", f"sort={sort}", f"limit={int(limit)}"]
    if t and t in _SEARCH_TIMEFRAMES and sort in ("top", "relevance"):
        params.append(f"t={t}")

    if sub and restrict_sr:
        encoded_sub = urllib.parse.quote(_normalize_sub(sub), safe="")
        params.insert(0, "restrict_sr=1")
        path = f"/r/{encoded_sub}/search.rss?" + "&".join(params)
    else:
        params.insert(0, "restrict_sr=off")
        path = "/search.rss?" + "&".join(params)

    return fetch_feed(f"https://{RSS_HOSTS[0]}{path}", timeout=timeout)


def fetch_subreddit_new(sub: str, limit: int = 25,
                        timeout: int = 15) -> list[dict[str, Any]]:
    """Fetch /r/<sub>/new/.rss and return normalized posts (newest-first).

    The Atom feed is a single page of the most recent ~25 items with no cursor,
    so there is no pagination contract. Returns [] on any fetch/parse failure.
    """
    encoded = urllib.parse.quote(_normalize_sub(sub), safe="")
    path = f"/r/{encoded}/new/.rss?limit={int(limit)}"

    root = fetch_xml_resilient(path, timeout=timeout)
    if root is None:
        return []

    posts: list[dict[str, Any]] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        p = parse_atom_entry(entry)
        if p:
            posts.append(p)
    return posts


def _fetch_delta_public(sub: str, last_seen_id: str | None,
                        max_limit: int = 50) -> list[dict[str, Any]]:
    """RSS path for fetch_delta. The Atom feed is a single newest-first page,
    so we fetch once, stop at last_seen_id, skip removed/locked, and cap at
    max_limit. Returns newest-first."""
    posts = fetch_subreddit_new(sub, limit=min(max_limit, 100))
    collected: list[dict[str, Any]] = []
    for p in posts:
        if last_seen_id and p["id"] == last_seen_id:
            break
        if p["removed"] or p["locked"]:
            continue
        collected.append(p)
        if len(collected) >= max_limit:
            break
    return collected


# ─── Public API ──────────────────────────────────────────────────────

def fetch_delta(sub: str, last_seen_id: str | None,
                max_limit: int = 50) -> list[dict[str, Any]]:
    """Daily-delta scan: posts newer than last_seen_id from /r/<sub>/new/.rss."""
    return _fetch_delta_public(sub, last_seen_id, max_limit=max_limit)


def fetch_post(url_or_id: str, timeout: int = 15) -> dict[str, Any] | None:
    """Fetch a single Reddit submission by URL or bare id via the keyless
    comments RSS feed (`/comments/<id>/.rss`).

    Accepts a full Reddit URL (any host variant: www, old, np, m), a comment
    deep-link (returns the PARENT submission, which is the right thing to judge),
    or a bare post id ('abc123' or 't3_abc123'). Reddit post ids are base-36
    lowercase; an uppercase paste is lowercased so it matches the feed.

    Goes through fetch_xml_resilient, inheriting the dual-host 403 failover
    (www -> old.reddit), the per-IP request throttle, x-ratelimit pacing, 429
    backoff, and the UA-only header discipline (NO Accept header). One GET per
    call (two only on a www 403 failover).

    The first <entry> in a comments feed is the submission (id 't3_...');
    comment entries ('t1_...') are dropped by parse_atom_entry's id guard. We
    select by id-match rather than position, so a feed that lists a comment
    before the submission still returns the submission, never a comment.

    Returns the normalized post dict (same shape as parse_atom_entry), or None
    when: the id cannot be extracted/validated (no network call is made), the
    feed is unreachable or rate-limited, or the feed carries no submission entry
    matching the id. Callers cannot distinguish unreachable from removed/empty
    (both map to None); for the judge skill that is enough (reach, or fall back
    to a pasted title+body).

    This is the keyless replacement for the dead fetch_json single-post path.
    """
    # Extract -> validate -> build URL -> fetch. Order is load-bearing: validate
    # the EXTRACTED id (not the raw input, which legitimately contains '/' and
    # ':'), and validate BEFORE interpolating into the path (injection guard).
    raw = (url_or_id or "").strip()
    m = re.search(r"/comments/([a-z0-9]+)", raw, re.I)
    post_id = m.group(1) if m else re.sub(r"^t3_", "", raw)
    post_id = post_id.lower()
    if not re.fullmatch(r"[a-z0-9]+", post_id):
        _log(f"fetch_post: bad id {url_or_id!r}")
        return None

    root = fetch_xml_resilient(f"/comments/{post_id}/.rss", timeout=timeout)
    if root is None:
        return None

    for entry in root.findall(f"{_ATOM_NS}entry"):
        p = parse_atom_entry(entry)
        if p and p["id"] == post_id:
            return p
    _log(f"fetch_post: no submission entry for {post_id}")
    return None


def fetch_user_about(username: str) -> dict[str, Any] | None:
    """Fetch a Redditor's public profile (karma + age).

    `/user/<x>/about.json` returns 403 for anonymous requests as of 2026-05-29,
    and the RSS surface carries no karma/age data, so this always returns None.
    Callers (author_vet) MUST treat None as "unknown" and fail open, never
    rejecting an OP for missing karma/age. The username guard is kept so the
    contract (reject hostile usernames before any network call) still holds.
    """
    if not _safe_username(username):
        return None
    # Karma/age are no longer obtainable without OAuth, which stays removed.
    return None


def fetch_user_recent_subs(username: str, limit: int = 100) -> dict[str, int] | None:
    """Histogram of which subs a user recently commented in (recent N items).

    Rebuilt from `/user/<x>/comments/.rss`: each <entry> carries a
    <category term="<sub>"> tag naming the subreddit of the comment. Used by
    author_vet to detect "wrong audience" authors (e.g. >80% activity in
    hustle-bro subs). Returns {subreddit_name: count} or None on failure.
    """
    safe = _safe_username(username)
    if not safe:
        return None
    path = f"/user/{safe}/comments/.rss?limit={int(limit)}"
    root = fetch_xml_resilient(path)
    if root is None:
        return None
    counts: dict[str, int] = {}
    for entry in root.findall(f"{_ATOM_NS}entry"):
        cat = entry.find(f"{_ATOM_NS}category")
        if cat is None:
            continue
        sub = (cat.get("term") or "").strip()
        if sub:
            counts[sub] = counts.get(sub, 0) + 1
    return counts
