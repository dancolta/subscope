"""reddit.fetch_search: keyless search.rss URL construction + reachability contract.

Mocks fetch_feed (the network boundary) and pins the URL fetch_search builds plus
the None(unreachable) vs [](reachable-but-empty) propagation that the per-mode
fetch logic in cli.py depends on. No network.
"""
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subscope.lib import reddit  # noqa: E402


@pytest.fixture
def cap(monkeypatch):
    """Capture URLs passed to fetch_feed; return a configurable result."""
    state = {"urls": [], "result": []}

    def _fake_feed(url, timeout=15):
        state["urls"].append(url)
        return state["result"]

    monkeypatch.setattr(reddit, "fetch_feed", _fake_feed)
    return state


def _q_of(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]


def test_within_sub_url_shape(cap):
    reddit.fetch_search("msp", "connectwise OR datto", sort="new", limit=25)
    url = cap["urls"][0]
    assert "/r/msp/search.rss" in url
    assert "restrict_sr=1" in url
    assert "sort=new" in url
    assert "limit=25" in url


def test_sort_top_includes_t(cap):
    reddit.fetch_search("msp", "x", sort="top", t="year")
    url = cap["urls"][0]
    assert "sort=top" in url and "t=year" in url


def test_sort_new_omits_t(cap):
    # Reddit ignores t= with sort=new; we must not send it.
    reddit.fetch_search("msp", "x", sort="new", t="year")
    assert "t=year" not in cap["urls"][0]


def test_invalid_sort_coerced_to_new(cap):
    reddit.fetch_search("msp", "x", sort="bogus")
    assert "sort=new" in cap["urls"][0]


def test_invalid_timeframe_dropped(cap):
    reddit.fetch_search("msp", "x", sort="top", t="decade")
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(cap["urls"][0]).query)
    assert "t" not in params  # invalid timeframe never reaches the URL


def test_query_truncated_to_max_len(cap):
    long_q = " ".join(f"term{i}" for i in range(300))  # well over 512 chars
    reddit.fetch_search("msp", long_q)
    assert len(_q_of(cap["urls"][0])) <= reddit.QUERY_MAX_LEN


def test_none_propagates(monkeypatch):
    monkeypatch.setattr(reddit, "fetch_feed", lambda url, timeout=15: None)
    assert reddit.fetch_search("msp", "x") is None


def test_empty_list_propagates(monkeypatch):
    monkeypatch.setattr(reddit, "fetch_feed", lambda url, timeout=15: [])
    assert reddit.fetch_search("msp", "x") == []


def test_empty_query_returns_empty_without_network(monkeypatch):
    called = {"n": 0}

    def _f(url, timeout=15):
        called["n"] += 1
        return []

    monkeypatch.setattr(reddit, "fetch_feed", _f)
    assert reddit.fetch_search("msp", "   ") == []
    assert called["n"] == 0  # no GET fired for an empty query


def test_global_search_when_sub_none(cap):
    reddit.fetch_search(None, "x")
    url = cap["urls"][0]
    assert "/search.rss" in url and "restrict_sr=off" in url
    assert "/r/" not in url


def test_keyless_host_only(cap):
    # No auth host ever (mirror test_no_oauth_surface posture).
    reddit.fetch_search("msp", "x")
    url = cap["urls"][0]
    assert any(url.startswith(f"https://{h}") for h in reddit.RSS_HOSTS)
    assert "oauth" not in url and "/api/v1" not in url


def test_sub_prefix_strip_not_charclass(cap):
    # Regression: lstrip("r/") mangled subs starting with 'r' (rails -> ails).
    reddit.fetch_search("rails", "x")
    assert "/r/rails/search.rss" in cap["urls"][0]


def test_sub_r_prefix_stripped(cap):
    reddit.fetch_search("r/recruiting", "x")
    assert "/r/recruiting/search.rss" in cap["urls"][0]


def test_sub_path_injection_encoded(cap):
    # Internal slashes must be percent-encoded so a hand-edited sub cannot traverse.
    reddit.fetch_search("evil/../../x", "q")
    url = cap["urls"][0]
    assert "evil%2F" in url
    assert "/../" not in url
