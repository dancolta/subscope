"""Batched multi-sub fetch + per-run GET budget.

Reddit's keyless RSS bucket serves ~1 request per 60s window per IP, shared
across www and old.reddit. Two behaviors follow, and both are load-bearing:

  1. A 200 that reports x-ratelimit-remaining=0 is the STEADY STATE, not a
     fault. It must pace and continue. Treating it as terminal ended every run
     after a single subreddit.
  2. One request per minute makes per-sub feeds unaffordable, so subs are
     batched into combined /r/a+b+c/new/.rss feeds and the run carries a hard
     GET budget so it can never become open-ended.
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subscope.lib import reddit  # noqa: E402

# This module IS the prefetch's test, so it opts out of the conftest stub and
# mocks the transport itself.
pytestmark = pytest.mark.live_prefetch


def _entry(post_id: str, sub: str, title: str = "a post") -> str:
    return f"""
      <entry>
        <id>t3_{post_id}</id>
        <category term="{sub}" label="r/{sub}"/>
        <title>{title}</title>
        <link href="https://www.reddit.com/r/{sub}/comments/{post_id}/x/"/>
        <published>2026-08-11T10:00:00+00:00</published>
        <author><name>/u/someone</name></author>
        <content type="html">body text</content>
      </entry>
    """


def _feed(*entries: str) -> ET.Element:
    return ET.fromstring(
        '<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>"
    )


def _sub(name: str, saturation: str = "medium") -> dict:
    return {"name": name, "tier": 1, "bucket": "operator",
            "weight": 1.0, "saturation": saturation}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    reddit.reset_fetch_stats()
    reddit._last_request_at = 0.0
    monkeypatch.setattr(reddit, "_sleep", lambda s: None)
    yield
    reddit.reset_fetch_stats()


# ─── Batch planning ───────────────────────────────────────────────────

def test_batches_pack_under_the_cost_cap():
    subs = [_sub("a", "high"), _sub("b", "medium"), _sub("c", "low"),
            _sub("d", "medium"), _sub("e", "high")]
    batches = reddit.plan_sub_batches(subs, cost_cap=6)
    assert all(batches), "no empty batch"
    for batch in batches:
        cost = sum(reddit.SATURATION_COST[s["saturation"]] for s in batch)
        assert cost <= 6
    # Every sub lands in exactly one batch, config order preserved.
    flat = [s["name"] for b in batches for s in b]
    assert flat == ["a", "b", "c", "d", "e"]


def test_batching_beats_one_request_per_sub():
    """The whole point: 10 subs must not cost 10 requests."""
    subs = [_sub(f"s{i}") for i in range(10)]
    batches = reddit.plan_sub_batches(subs, cost_cap=6)
    assert len(batches) < len(subs)


def test_sub_over_the_cap_still_gets_a_batch():
    """A single sub costing more than the cap must be fetched, not dropped."""
    batches = reddit.plan_sub_batches([_sub("huge", "high")], cost_cap=1)
    assert [s["name"] for b in batches for s in b] == ["huge"]


# ─── Combined feed ────────────────────────────────────────────────────

def test_multi_feed_is_one_request_split_by_category():
    root = _feed(_entry("p1", "alpha"), _entry("p2", "beta"), _entry("p3", "alpha"))
    with patch.object(reddit, "fetch_xml_resilient", return_value=root) as m:
        by_sub = reddit.fetch_multi_new(["alpha", "beta"])
    assert m.call_count == 1
    path = m.call_args[0][0]
    assert path.startswith("/r/alpha+beta/new/.rss")
    assert [p["id"] for p in by_sub["alpha"]] == ["p1", "p3"]
    assert [p["id"] for p in by_sub["beta"]] == ["p2"]


def test_multi_feed_matches_sub_names_case_insensitively():
    """Config spelling need not match Reddit's canonical <category term>."""
    root = _feed(_entry("p1", "SideProject"))
    with patch.object(reddit, "fetch_xml_resilient", return_value=root):
        by_sub = reddit.fetch_multi_new(["sideproject"])
    assert [p["id"] for p in by_sub["sideproject"]] == ["p1"]


def test_multi_feed_returns_empty_list_for_a_sub_with_no_entries():
    root = _feed(_entry("p1", "alpha"))
    with patch.object(reddit, "fetch_xml_resilient", return_value=root):
        by_sub = reddit.fetch_multi_new(["alpha", "quiet"])
    assert by_sub["quiet"] == []


def test_multi_feed_unreachable_is_none_not_empty():
    """None (unreachable) must stay distinguishable from a quiet batch."""
    with patch.object(reddit, "fetch_xml_resilient", return_value=None):
        assert reddit.fetch_multi_new(["alpha"]) is None


# ─── Prefetch feeding fetch_delta ─────────────────────────────────────

def test_prime_makes_fetch_delta_free():
    root = _feed(_entry("p1", "alpha"), _entry("p2", "beta"))
    with patch.object(reddit, "fetch_xml_resilient", return_value=root) as m:
        unfetched = reddit.prime_new_cache([_sub("alpha"), _sub("beta")])
        before = m.call_count
        posts = reddit.fetch_delta("alpha", None, max_limit=25)
    assert unfetched == []
    assert m.call_count == before, "fetch_delta must not hit the network when primed"
    assert [p["id"] for p in posts] == ["p1"]


def test_primed_delta_still_honors_the_cursor():
    root = _feed(_entry("new1", "alpha"), _entry("seen", "alpha"),
                 _entry("old", "alpha"))
    with patch.object(reddit, "fetch_xml_resilient", return_value=root):
        reddit.prime_new_cache([_sub("alpha")])
        posts = reddit.fetch_delta("alpha", "seen", max_limit=25)
    assert [p["id"] for p in posts] == ["new1"]


def test_unfetched_subs_are_reported_not_silently_quiet():
    with patch.object(reddit, "fetch_xml_resilient", return_value=None):
        unfetched = reddit.prime_new_cache([_sub("alpha"), _sub("beta")])
    assert set(unfetched) == {"alpha", "beta"}


def test_unprimed_sub_falls_back_to_its_own_feed():
    root = _feed(_entry("p1", "alpha"))
    with patch.object(reddit, "fetch_xml_resilient", return_value=root) as m:
        posts = reddit.fetch_delta("alpha", None, max_limit=25)
    assert m.call_count == 1
    assert [p["id"] for p in posts] == ["p1"]


# ─── Request budget ───────────────────────────────────────────────────

class _Resp:
    """Minimal urlopen stand-in, so the real budget accounting runs."""

    def __init__(self, body: str, headers: dict | None = None):
        self._body = body.encode()
        self.headers = headers or {}
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_budget_caps_requests_and_reports_the_rest_unfetched():
    body = '<feed xmlns="http://www.w3.org/2005/Atom">' + _entry("p1", "s0") + "</feed>"
    reddit.set_request_budget(1)
    subs = [_sub(f"s{i}", "high") for i in range(6)]  # one sub per batch
    with patch.object(reddit.urllib.request, "urlopen",
                      side_effect=lambda *a, **k: _Resp(body)) as m:
        unfetched = reddit.prime_new_cache(subs, cost_cap=1)
    assert m.call_count == 1, "budget must stop the run, not just slow it"
    assert len(unfetched) == 5
    assert reddit.requests_used() == 1


def test_budget_skip_is_not_a_reachability_failure():
    """A skipped GET must not look like a blocked feed, or the run would report
    'blocked' when nothing was actually wrong with Reddit."""
    reddit.set_request_budget(0)
    assert reddit.fetch_xml("https://www.reddit.com/r/a/new/.rss") is None
    assert reddit.get_fetch_stats()["failed"] == 0
    assert reddit.get_fetch_stats()["ok"] == 0


def test_budget_counts_retries():
    """Retries are real GETs against the same bucket, so they are charged."""
    err = reddit.urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "1"}, None)
    reddit.set_request_budget(10)
    with patch.object(reddit.urllib.request, "urlopen", side_effect=err):
        reddit.fetch_xml("https://www.reddit.com/r/a/new/.rss")
    assert reddit.requests_used() == reddit.MAX_RETRIES


def test_reset_clears_a_spent_budget():
    """A spent budget must never leak into the next batch and mute its requests."""
    reddit.set_request_budget(0)
    assert reddit.budget_exhausted() is True
    reddit.reset_fetch_stats()
    assert reddit.budget_exhausted() is False
    assert reddit.budget_remaining() is None


def test_no_budget_means_unlimited():
    reddit.reset_fetch_stats()
    assert reddit.budget_remaining() is None
    assert reddit.budget_exhausted() is False


# ─── Rate-limit semantics (the bug that broke every run) ──────────────

def test_remaining_zero_on_a_200_paces_and_keeps_going():
    """Reddit reports remaining=0 on the first 200 of every window. If that
    flips a terminal flag, a run reads exactly one subreddit and quits."""
    reddit._ratelimit_pause_from_headers(
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "30"})
    assert reddit.is_rate_limited() is False


def test_batching_stops_once_a_real_429_drains_the_bucket():
    reddit._RATE_STATE["drained"] = True
    with patch.object(reddit, "fetch_xml_resilient") as m:
        unfetched = reddit.prime_new_cache([_sub("a"), _sub("b")], cost_cap=1)
    assert m.call_count == 0
    assert set(unfetched) == {"a", "b"}


# ─── fetch.yml strategy selection ─────────────────────────────────────

def _write_min_config(d):
    (d / "subreddits.yml").write_text(
        "subreddits:\n  - {name: alpha, tier: 1, bucket: operator, weight: 1.0}\n")
    (d / "keywords.yml").write_text("shared: [automate]\noperator: []\nbuilder: []\n")


def test_fetch_strategy_defaults_to_new(tmp_path, monkeypatch):
    """No fetch.yml means the /new path, unchanged for every existing profile."""
    from subscope import cli
    _write_min_config(tmp_path)
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    assert cli._load_configs()["strategy"] == "new"


def test_fetch_strategy_search_is_read_from_config(tmp_path, monkeypatch):
    from subscope import cli
    _write_min_config(tmp_path)
    (tmp_path / "fetch.yml").write_text("strategy: search\n")
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    assert cli._load_configs()["strategy"] == "search"


def test_unknown_fetch_strategy_is_rejected_loudly(tmp_path, monkeypatch):
    """A typo must fail the run, not silently fall back to /new and quietly
    return the wrong kind of post for the whole profile."""
    from subscope import cli
    _write_min_config(tmp_path)
    (tmp_path / "fetch.yml").write_text("strategy: serch\n")
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError, match="serch"):
        cli._load_configs()
