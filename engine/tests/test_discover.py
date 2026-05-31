"""Tests for live subreddit discovery (engine/subscope/lib/discover.py).

Covers all five spec concerns in one file:
1. derive_queries — query construction from interview answers + scrape + competitors
2. search_dfs — DFS SERP harvest (mocks enrich.dfs_serp_advanced)
3. search_reddit — Reddit native search (mocks reddit.fetch_feed)
4. rank_subs — scoring, noise downrank, freshness cutoff
5. fallback + e2e — needs_clarification triggers, discovery_unreachable, full flow

Mocks are surgical: we patch the HTTP seams (enrich.dfs_serp_advanced /
reddit.fetch_feed) not the higher-level functions, so the test exercises the
real harvest + dedup + scoring code paths.
"""
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subscope.lib import discover, enrich, reddit, store  # noqa: E402


NOW = int(time.time())


@pytest.fixture(autouse=True)
def _stub_subreddit_directory(monkeypatch):
    """search_subreddits() hits reddit.fetch_xml_resilient, a network seam the
    per-test fetch_feed mocks do not cover. Default it to no-op so the existing
    discovery tests stay hermetic (directory recall returns nothing unless a
    test explicitly patches it)."""
    monkeypatch.setattr(reddit, "fetch_xml_resilient", lambda *a, **k: None)


# ─── Helpers ───────────────────────────────────────────────────────────


def _conn():
    """In-memory SQLite with enrichment_cache table installed."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store._ensure_enrichment_cache_table(c)
    return c


def _reddit_search_response(*threads_args) -> list[dict]:
    """Build a fetch_feed-shaped result: a list of normalized RSS posts (the
    shape reddit.fetch_feed returns). Carries both the search-path keys
    (subreddit/score/num_comments/url) and the Phase-B keys (id/title/body/
    created_utc/url) so it works for both search_reddit and _fetch_candidate_posts.
    Titles include a buyer-intent token so threads pass the intent gate."""
    posts = []
    for sub, score, num_comments, age_days in threads_args:
        posts.append({
            "id": f"t3_{sub}_{age_days}",
            "subreddit": sub,
            "title": f"alternative to thing in {sub}?",
            "score": score,
            "num_comments": num_comments,
            "created_utc": NOW - age_days * 86400,
            "body": "",
            "url": f"/r/{sub}/comments/abc123/",
        })
    return posts


def _dfs_serp_response(*urls) -> dict:
    """Build a dfs_serp_advanced-shaped payload. Snippets include a buyer-intent
    token so threads pass the intent gate."""
    return {
        "query": "test",
        "items": [{"url": u, "title": f"title for {u}",
                   "snippet": "looking for an alternative"} for u in urls],
    }


# ─── 1. derive_queries ────────────────────────────────────────────────


def test_derive_queries_dan_real_case():
    """The exact case Dan ran: AI automations / SME founders / saas price + cold email."""
    answers = {
        "what_offering": "ai automations and replacements for saas subscriptions",
        "who_to_reach": "founders of sme's 5-15 people",
        "pain_quote": "saas price increased, expensive infrastructure for cold email outreaches",
    }
    queries = discover.derive_queries(answers, scrape_markdown=None,
                                      competitors=["instantly.ai", "smartlead.ai"])
    # Must include the verbatim pain (rule 1) and price-rage anchor (rule 4)
    assert any("saas price increased" in q for q in queries)
    assert any("price increase alternatives" in q for q in queries)
    # Competitors get cleaned and queried (rule 3)
    assert any("instantly" in q for q in queries)
    # Capped at MAX_QUERIES
    assert len(queries) <= discover.MAX_QUERIES


def test_derive_queries_empty_answers_returns_empty():
    queries = discover.derive_queries({"what_offering": "", "who_to_reach": "",
                                       "pain_quote": ""})
    assert queries == []


def test_derive_queries_jaccard_dedup():
    """Two near-identical inputs should not produce duplicate queries."""
    answers = {
        "what_offering": "saas tool",
        "who_to_reach": "founders",
        "pain_quote": "saas tool too expensive",
    }
    queries = discover.derive_queries(answers)
    # Rule 1 ("saas tool too expensive") and rule 5 (offering pain
    # "saas tool too expensive") would collide — Jaccard dedup must catch it
    norm = [q.replace(" ", "") for q in queries]
    assert len(set(norm)) == len(norm), f"duplicate queries: {queries}"


def test_derive_queries_vertical_clarifier_adds_query():
    answers = {
        "what_offering": "automation",
        "who_to_reach": "founders",
        "pain_quote": "tools cost too much",
    }
    base = discover.derive_queries(answers)
    with_vertical = discover.derive_queries(answers, vertical="accounting")
    assert any("accounting" in q for q in with_vertical)
    assert len(with_vertical) >= len(base)


def test_derive_queries_no_company_name_injection():
    """Company name from T2 must not bleed into the queries."""
    answers = {
        "what_offering": "nodesparks helps you automate everything",
        "who_to_reach": "saas founders",
        "pain_quote": "manual work is killing us",
    }
    queries = discover.derive_queries(answers)
    # We don't extract company names, but we do extract offering pain. Make
    # sure no query is literally the company name.
    for q in queries:
        assert "nodesparks" not in q, f"company name leaked into query: {q}"


def test_derive_queries_homepage_pain_extraction():
    answers = {"what_offering": "x", "who_to_reach": "y", "pain_quote": "billing pain"}
    md = "We help teams tired of paying for legacy CRM tools every month."
    queries = discover.derive_queries(answers, scrape_markdown=md)
    assert any("paying for legacy crm tools" in q for q in queries), queries


# ─── 2. search_dfs ────────────────────────────────────────────────────


def test_search_dfs_harvests_subs_from_urls():
    c = _conn()
    resp = _dfs_serp_response(
        "https://www.reddit.com/r/microsaas/comments/abc/foo/",
        "https://reddit.com/r/Bookkeeping/comments/def/bar/",
        "https://example.com/not-reddit",
        "https://www.reddit.com/r/SaaS/comments/xyz/baz/",
    )
    with patch.object(enrich, "dfs_serp_advanced", return_value=resp):
        threads = discover.search_dfs("test query", c)
    subs = [t["sub"] for t in threads]
    assert "microsaas" in subs
    assert "Bookkeeping" in subs
    assert "SaaS" in subs
    # Non-reddit URL skipped
    assert all("not-reddit" not in t.get("permalink", "") for t in threads)


def test_search_dfs_empty_query_returns_empty():
    c = _conn()
    assert discover.search_dfs("", c) == []


def test_search_dfs_disabled_returns_empty():
    """When dfs_serp_advanced returns None (creds absent), discover handles it."""
    c = _conn()
    with patch.object(enrich, "dfs_serp_advanced", return_value=None):
        threads = discover.search_dfs("anything", c)
    assert threads == []


# ─── 3. search_reddit ─────────────────────────────────────────────────


def test_search_reddit_extracts_threads():
    # Each thread needs a buyer-intent title or it's filtered out.
    feed = [
        {"subreddit": "microsaas", "title": "alternative to Mailchimp?",
         "score": 50, "num_comments": 12, "created_utc": NOW - 30 * 86400,
         "url": "/r/microsaas/comments/a/x/"},
        {"subreddit": "EntrepreneurRideAlong", "title": "switching from Apollo",
         "score": 80, "num_comments": 5, "created_utc": NOW - 60 * 86400,
         "url": "/r/Era/comments/b/y/"},
        {"subreddit": "SaaS", "title": "looking for cheaper CRM",
         "score": 200, "num_comments": 40, "created_utc": NOW - 14 * 86400,
         "url": "/r/SaaS/comments/c/z/"},
    ]
    with patch.object(reddit, "fetch_feed", return_value=feed):
        threads = discover.search_reddit("cold email expensive", sleep_between=0)
    subs = [t["sub"] for t in threads]
    assert subs == ["microsaas", "EntrepreneurRideAlong", "SaaS"]
    assert all(t["source"] == "reddit_native" for t in threads)


def test_search_reddit_filters_non_intent_titles():
    """Thread titles without buyer-intent tokens (alternative/switch/replace/etc)
    must be dropped before harvest. This is the bigger fix from live smoke:
    r/wallstreetbets matched 'saas subscriptions too expensive' as stock chatter."""
    feed = [
        # Has intent → kept
        {"subreddit": "microsaas", "title": "alternatives to Klaviyo?",
         "score": 10, "num_comments": 2, "created_utc": NOW - 30 * 86400,
         "url": "/x/"},
        # No intent token → dropped (stock analysis lexical match)
        {"subreddit": "wallstreetbets", "title": "SaaS subscriptions are too expensive, sell SF",
         "score": 5000, "num_comments": 800, "created_utc": NOW - 7 * 86400,
         "url": "/y/"},
        # No intent token → dropped (venting)
        {"subreddit": "mildlyinfuriating", "title": "my saas keeps raising prices",
         "score": 100, "num_comments": 30, "created_utc": NOW - 3 * 86400,
         "url": "/z/"},
    ]
    with patch.object(reddit, "fetch_feed", return_value=feed):
        threads = discover.search_reddit("saas subscriptions expensive", sleep_between=0)
    subs = [t["sub"] for t in threads]
    assert subs == ["microsaas"]


def test_has_buyer_intent_token_matrix():
    """Spot-check the regex word-boundary matching."""
    assert discover._has_buyer_intent("alternative to Mailchimp")
    assert discover._has_buyer_intent("Switching from Apollo, recommendations?")
    assert discover._has_buyer_intent("Looking for a cheaper CRM")
    assert discover._has_buyer_intent("Anyone use Smartlead instead of Instantly?")
    # Negative cases
    assert not discover._has_buyer_intent("My saas keeps raising prices")
    assert not discover._has_buyer_intent("Just venting about pricing today")
    assert not discover._has_buyer_intent("")


def test_noise_denylist_includes_finance_and_venting():
    """The live smoke caught wallstreetbets + mildlyinfuriating as noise.
    Both must now be in NOISE_DOWNRANK_SUBS (lowercase)."""
    for sub in ("wallstreetbets", "valueinvesting", "mildlyinfuriating",
                "middleclasshq", "layoffs", "personalfinance"):
        assert sub in discover.NOISE_DOWNRANK_SUBS, f"missing from denylist: {sub}"


def test_search_reddit_rejects_bad_sub_names():
    """Sub names that don't match Reddit's rules must be dropped."""
    # All titles include a buyer-intent token so the intent gate isn't the
    # discriminator; this test is specifically about sub-name validation.
    feed = [
        {"subreddit": "fine_sub", "title": "alternative to Mailchimp?", "score": 1,
         "num_comments": 0, "created_utc": NOW - 86400, "url": "/r/fine_sub/c/x/"},
        {"subreddit": "this-has-dashes-too-long-for-reddit",
         "title": "switching from Apollo",
         "score": 1, "num_comments": 0, "created_utc": NOW, "url": ""},
        {"subreddit": "", "title": "looking for cheaper CRM", "score": 0,
         "num_comments": 0, "created_utc": NOW, "url": ""},
    ]
    with patch.object(reddit, "fetch_feed", return_value=feed):
        threads = discover.search_reddit("x", sleep_between=0)
    assert [t["sub"] for t in threads] == ["fine_sub"]


def test_search_reddit_network_failure_returns_empty():
    with patch.object(reddit, "fetch_feed", return_value=None):
        threads = discover.search_reddit("anything", sleep_between=0)
    assert threads == []


# ─── 4. rank_subs ─────────────────────────────────────────────────────


def test_rank_subs_orders_by_frequency_first():
    """A sub appearing in 3 distinct query results should rank higher than
    a sub appearing once with higher per-thread quality."""
    threads = [
        # 3 distinct queries hit "microsaas" — frequency wins
        {"sub": "microsaas", "score": 10, "num_comments": 1, "created_utc": NOW - 86400,
         "source_query": "q1", "source": "reddit_native"},
        {"sub": "microsaas", "score": 10, "num_comments": 1, "created_utc": NOW - 86400,
         "source_query": "q2", "source": "reddit_native"},
        {"sub": "microsaas", "score": 10, "num_comments": 1, "created_utc": NOW - 86400,
         "source_query": "q3", "source": "reddit_native"},
        # 1 query hit "shinyhighscore" with very high upvotes
        {"sub": "shinyhighscore", "score": 5000, "num_comments": 200,
         "created_utc": NOW - 86400, "source_query": "q1", "source": "reddit_native"},
    ]
    ranked = discover.rank_subs(threads)
    assert ranked[0]["name"] == "microsaas"


def test_rank_subs_applies_noise_downrank():
    """r/SaaS (noise) with same raw signal as r/microsaas (clean) must rank lower."""
    threads = []
    for sub in ("microsaas", "SaaS"):
        for q in ("q1", "q2"):
            threads.append({
                "sub": sub, "score": 50, "num_comments": 10,
                "created_utc": NOW - 7 * 86400,
                "source_query": q, "source": "reddit_native",
            })
    ranked = discover.rank_subs(threads)
    by_name = {r["name"]: r for r in ranked}
    assert by_name["microsaas"]["score"] > by_name["SaaS"]["score"]
    assert by_name["SaaS"]["noise_downranked"] is True
    assert by_name["microsaas"]["noise_downranked"] is False


def test_rank_subs_drops_threads_older_than_cutoff():
    # Each sub needs MIN_THREAD_COUNT (2) threads so they survive the
    # single-thread gate. The point of THIS test is the age filter.
    threads = [
        {"sub": "old", "score": 50, "num_comments": 10,
         "created_utc": NOW - 1000 * 86400,
         "source_query": "q1", "source": "reddit_native"},
        {"sub": "old", "score": 50, "num_comments": 10,
         "created_utc": NOW - 1100 * 86400,
         "source_query": "q2", "source": "reddit_native"},
        {"sub": "fresh", "score": 10, "num_comments": 1,
         "created_utc": NOW - 30 * 86400,
         "source_query": "q1", "source": "reddit_native"},
        {"sub": "fresh", "score": 10, "num_comments": 1,
         "created_utc": NOW - 40 * 86400,
         "source_query": "q2", "source": "reddit_native"},
    ]
    ranked = discover.rank_subs(threads)
    assert [r["name"] for r in ranked] == ["fresh"]


def test_rank_subs_case_insensitive_aggregation():
    """`microSaaS` and `microsaas` should aggregate into one bucket."""
    threads = [
        {"sub": "MicroSaaS", "score": 10, "num_comments": 1,
         "created_utc": NOW - 86400, "source_query": "q1", "source": "reddit_native"},
        {"sub": "microsaas", "score": 10, "num_comments": 1,
         "created_utc": NOW - 86400, "source_query": "q2", "source": "reddit_native"},
    ]
    ranked = discover.rank_subs(threads)
    assert len(ranked) == 1
    assert ranked[0]["thread_count"] == 2


def test_rank_subs_why_line_format():
    # Two threads from the same sub matching different queries — exactly the
    # cross-query confirmation pattern the ranker now requires.
    threads = [
        {"sub": "Bookkeeping", "score": 50, "num_comments": 10,
         "created_utc": NOW - 86400,
         "source_query": "saas price increase alternatives", "source": "reddit_native"},
        {"sub": "Bookkeeping", "score": 30, "num_comments": 5,
         "created_utc": NOW - 86400,
         "source_query": "switching from quickbooks", "source": "reddit_native"},
    ]
    ranked = discover.rank_subs(threads)
    # Why-line uses the highest-engagement thread's source_query
    assert ranked[0]["why"] == "found in 2 threads matching 'saas price increase alternatives'"


# ─── 5. Fallback + end-to-end ─────────────────────────────────────────


def test_e2e_dan_case_returns_subs_and_no_clarification():
    """End-to-end on Dan's real input. Mock both providers with realistic
    responses. Expect non-noise subs in the top of the ranking."""
    c = _conn()
    answers = {
        "what_offering": "ai automations and replacements for saas subscriptions",
        "who_to_reach": "founders of sme's 5-15 people",
        "pain_quote": "saas price increased, expensive infrastructure for cold email outreaches",
    }

    def fake_fetch_json(url, timeout=15):
        # Return a varied set of subs per query, with non-noise dominating
        if "saas+price+increased" in url or "saas%20price%20increased" in url:
            return _reddit_search_response(
                ("microsaas", 80, 20, 14),
                ("EntrepreneurRideAlong", 50, 15, 30),
                ("Bookkeeping", 40, 10, 21),
            )
        if "instantly" in url.lower():
            return _reddit_search_response(
                ("coldemail", 30, 8, 60),
                ("EmailMarketing", 60, 12, 45),
                ("smallbusiness", 100, 20, 30),
            )
        # default: mixed
        return _reddit_search_response(
            ("automation", 25, 5, 14),
            ("SaaS", 200, 50, 7),
            ("smallbusinessIT", 15, 3, 90),
        )

    # v3: also mock Phase B validation. Pass for all non-noise subs so the
    # e2e flow can verify Phase A + ranking still works. Phase B is tested
    # separately below.
    def fake_phase_b(sub_name, user_vocab, competitors, **kw):
        return {
            "fresh_post_count": 5, "fresh_buyer_intent_count": 3,
            "fresh_relevance_count": 2, "weighted_relevance": 2.0,
            "relevance_path": "competitor",
            "recent_thread_url": f"https://reddit.com/r/{sub_name}/comments/x/",
            "recent_thread_title": f"alternative to {sub_name}",
            "recent_thread_age_h": 12.0, "recent_thread_iso": "2026-05-28 10:00 UTC",
            "passed": True, "timed_out": False,
            "error": None,
        }

    with patch.object(reddit, "fetch_feed", side_effect=fake_fetch_json):
        with patch.object(enrich, "detect_providers", return_value={"dataforseo": False,
                                                                    "firecrawl": False}):
            with patch.object(discover, "validate_sub_freshness", side_effect=fake_phase_b):
                result = discover.discover_subs_for_profile(
                    answers, "https://nodesparks.com", c,
                )

    assert result["needs_clarification"] is False
    assert len(result["subs"]) >= discover.MIN_SUBS_THRESHOLD
    # Top sub should not be r/SaaS (noise downrank in effect)
    assert result["subs"][0]["name"].lower() != "saas"
    # Queries used must be reported
    assert len(result["queries_used"]) >= 3
    assert result["source_mix"]["reddit_native"] > 0
    # v3.2: every surfaced sub gets a confidence + recent_thread_url. Engine is
    # the recall stage; it surfaces gate-passers above the low floor (precision
    # comes from the skill-layer relevance review).
    for s in result["subs"]:
        assert "confidence" in s and isinstance(s["confidence"], int)
        assert s["confidence"] >= discover.CONFIDENCE_FLOOR
        assert "recent_thread_url" in s


def test_e2e_thin_results_trigger_clarification():
    """When only 1-2 non-noise subs come back, needs_clarification is set."""
    c = _conn()
    answers = {
        "what_offering": "thing",
        "who_to_reach": "people",
        "pain_quote": "it costs a lot",
    }
    # Every query returns only noise-list subs
    noise_resp = _reddit_search_response(
        ("SaaS", 50, 10, 14),
        ("Entrepreneur", 30, 5, 21),
    )
    with patch.object(reddit, "fetch_feed", return_value=noise_resp):
        with patch.object(enrich, "detect_providers", return_value={"dataforseo": False,
                                                                    "firecrawl": False}):
            result = discover.discover_subs_for_profile(answers, "", c)
    assert result["needs_clarification"] is True
    assert result["clarifier_prompt"] is not None


def _named_threads(sub, n, *, names="", body=""):
    """Threads for one sub. `names`/`body` injected so competitor / vocab signal
    is controllable. source_query cycles 2 values so freq>=2 (clears the Phase A
    admit gate) when rank_subs is called directly without search_reddit tagging."""
    return [{
        "id": f"t3_{sub}_{i}", "subreddit": sub, "sub": sub,
        "title": f"{names} thread {i} in {sub}", "score": 10, "num_comments": 2,
        "created_utc": NOW - 2 * 86400, "body": body,
        "url": f"/r/{sub}/comments/{i}/", "source_query": f"q{i % 2}",
    } for i in range(n)]


def test_rank_subs_emits_offer_signals():
    """rank_subs exposes per-sub freq / vocab_match / competitor_match so the
    skip path can gate on a real offer signal."""
    threads = _named_threads("CRMSoftware", 2, names="HubSpot vs Pipedrive", body="comparing hubspot")
    ranked = discover.rank_subs(threads, user_vocab={"crm"},
                                comp_tokens={"hubspot", "pipedrive"})
    assert ranked, "expected at least one ranked sub"
    e = ranked[0]
    assert "freq" in e and "vocab_match" in e and "competitor_match" in e
    assert e["competitor_match"] is True       # threads name HubSpot
    # No comp_tokens -> competitor_match defaults False (backward compat)
    ranked2 = discover.rank_subs(threads, user_vocab={"crm"})
    assert ranked2[0]["competitor_match"] is False


def test_skip_validation_drops_generic_only_garbage():
    """The regression: a sub admitted to Phase A only via freq>=2 on generic
    query tokens (no vocab, no competitor) is DROPPED in the skip path, while a
    competitor-naming sub is kept."""
    c = _conn()
    answers = {"what_offering": "CRM for small B2B sales teams",
               "who_to_reach": "sales leaders", "pain_quote": "salesforce is too expensive"}
    # r/weddingshaming: generic-token match only, no competitor, no vocab.
    # r/CRMSoftware: threads name HubSpot (a competitor) -> real offer signal.
    resp = (_named_threads("weddingshaming", 3, names="alternative drama expensive")
            + _named_threads("CRMSoftware", 3, names="HubSpot alternative", body="moving off hubspot"))
    with patch.object(reddit, "fetch_feed", return_value=resp):
        with patch.object(enrich, "detect_providers",
                          return_value={"dataforseo": False, "firecrawl": False}):
            result = discover.discover_subs_for_profile(
                answers, "", c, skip_validation=True, extra_competitors=["HubSpot", "Salesforce"],
            )
    names = [s["name"].lower() for s in result["subs"]]
    assert "crmsoftware" in names, "competitor-naming sub must be kept"
    assert "weddingshaming" not in names, "generic-only garbage sub must be dropped"
    for s in result["subs"]:
        assert isinstance(s["relevance"], int) and 0 <= s["relevance"] <= 100
        assert s["validation_skipped"] is True
        assert "confidence" not in s


def test_skip_validation_all_garbage_triggers_clarification():
    """If nothing has a real offer signal, ask for the vertical instead of
    showing freq-only noise."""
    c = _conn()
    answers = {"what_offering": "CRM for sales teams", "who_to_reach": "sales",
               "pain_quote": "too expensive"}
    resp = (_named_threads("weddingshaming", 3, names="alternative expensive drama")
            + _named_threads("HFY", 3, names="alternative expensive story"))
    with patch.object(reddit, "fetch_feed", return_value=resp):
        with patch.object(enrich, "detect_providers",
                          return_value={"dataforseo": False, "firecrawl": False}):
            result = discover.discover_subs_for_profile(
                answers, "", c, skip_validation=True,
            )
    assert result["subs"] == []
    assert result["needs_clarification"] is True
    assert result["validation_skipped"] is True


def test_skip_validation_keeps_vocab_match_no_competitor():
    """A sub whose name matches the offer vocabulary is kept even with no
    competitor named (vocab is a valid offer signal)."""
    c = _conn()
    answers = {"what_offering": "bookkeeping software", "who_to_reach": "bookkeepers",
               "pain_quote": "quickbooks raised prices"}
    resp = _named_threads("Bookkeeping", 3, names="software recommendation question")
    with patch.object(reddit, "fetch_feed", return_value=resp):
        with patch.object(enrich, "detect_providers",
                          return_value={"dataforseo": False, "firecrawl": False}):
            result = discover.discover_subs_for_profile(
                answers, "", c, skip_validation=True,
            )
    names = [s["name"].lower() for s in result["subs"]]
    assert "bookkeeping" in names


def test_e2e_no_provider_response_flags_discovery_unreachable():
    """When fetch_json returns None for everything, discovery_unreachable=True."""
    c = _conn()
    answers = {
        "what_offering": "automation tool",
        "who_to_reach": "ops leaders",
        "pain_quote": "manual work is killing us",
    }
    with patch.object(reddit, "fetch_feed", return_value=None):
        with patch.object(enrich, "detect_providers", return_value={"dataforseo": False,
                                                                    "firecrawl": False}):
            result = discover.discover_subs_for_profile(answers, "", c)
    assert result["discovery_unreachable"] is True
    assert result["subs"] == []


def test_e2e_vertical_param_suppresses_second_clarification():
    """If vertical was already provided (Tier-A retry), we don't ask again."""
    c = _conn()
    answers = {
        "what_offering": "thing",
        "who_to_reach": "people",
        "pain_quote": "it costs",
    }
    noise_resp = _reddit_search_response(("SaaS", 50, 10, 14))
    with patch.object(reddit, "fetch_feed", return_value=noise_resp):
        with patch.object(enrich, "detect_providers", return_value={"dataforseo": False,
                                                                    "firecrawl": False}):
            result = discover.discover_subs_for_profile(
                answers, "", c, vertical="accounting",
            )
    assert result["needs_clarification"] is False


# ─── Misc: regex / hygiene ─────────────────────────────────────────────


def test_normalize_query_trims_leading_pronoun():
    assert discover._normalize_query("I'm tired of bad CRM") == "tired of bad crm"
    assert discover._normalize_query("We're paying too much") == "paying too much"


def test_normalize_query_word_boundary_trim():
    long_input = "this is a very long pain phrase that exceeds the maximum allowed query length cap"
    out = discover._normalize_query(long_input)
    assert len(out) <= 80
    # Must end on a word boundary, not mid-word
    assert not out.endswith(" ")


# ─── v3: Phase B validation tests ─────────────────────────────────────


def _new_json_response(*posts) -> list[dict]:
    """Build a fetch_feed-shaped result (normalized RSS posts) from
    (title, body, age_hours) tuples. _fetch_candidate_posts maps the post body
    to selftext and url to permalink for validate_sub_freshness."""
    out = []
    for title, body, age_h in posts:
        slug = title[:20].replace(" ", "_")
        out.append({
            "id": f"t3_{slug}",
            "title": title,
            "body": body,
            "created_utc": int(time.time() - age_h * 3600),
            "url": f"/r/foo/comments/abc/{slug}/",
        })
    return out


def test_phase_b_pass_when_fresh_intent_and_relevance():
    """Sub passes when at least one post in 48h has buyer-intent + vocab/competitor match."""
    resp = _new_json_response(
        ("Looking for alternative to Salesforce", "any recommendations?", 12.0),
        ("Just venting about prices", "ugh", 5.0),  # no intent
        ("Old thread", "switching from X", 240.0),  # too old (>48h)
    )
    with patch.object(reddit, "fetch_feed", return_value=resp):
        result = discover.validate_sub_freshness(
            "salesforce", user_vocab={"salesforce", "automation"},
            competitors=["Salesforce", "HubSpot"],
        )
    assert result["passed"] is True
    assert result["fresh_buyer_intent_count"] == 1  # only the alternative post
    assert result["fresh_relevance_count"] == 1
    assert result["recent_thread_age_h"] is not None
    assert result["recent_thread_age_h"] < 48


def test_phase_b_fail_when_no_fresh_posts():
    """All posts older than 48h = fail."""
    resp = _new_json_response(
        ("Looking for alternative to Salesforce", "switching", 100.0),
        ("Cheaper option needed", "help", 200.0),
    )
    with patch.object(reddit, "fetch_feed", return_value=resp):
        result = discover.validate_sub_freshness(
            "salesforce", user_vocab={"salesforce"}, competitors=["Salesforce"],
        )
    assert result["passed"] is False
    assert result["fresh_post_count"] == 0


def test_phase_b_fail_when_no_buyer_intent():
    """Fresh posts but none have buyer-intent tokens."""
    resp = _new_json_response(
        ("Just saying hi", "first post here", 5.0),
        ("Random thought", "thinking about salesforce stuff", 10.0),
    )
    with patch.object(reddit, "fetch_feed", return_value=resp):
        result = discover.validate_sub_freshness(
            "salesforce", user_vocab={"salesforce"}, competitors=["Salesforce"],
        )
    assert result["passed"] is False
    assert result["fresh_relevance_count"] == 0


def test_phase_b_fail_when_no_relevance():
    """Fresh + intent but no vocab or competitor match = fail (noise)."""
    resp = _new_json_response(
        ("Looking for alternative cosmetics", "want to switch brands", 5.0),
    )
    with patch.object(reddit, "fetch_feed", return_value=resp):
        result = discover.validate_sub_freshness(
            "salesforce", user_vocab={"crm", "salesforce"},
            competitors=["Salesforce"],
        )
    # v3.1: "alternative cosmetics" has intent ('alternative','switch') but no
    # product noun nearby and no competitor brand -> must NOT pass. This is the
    # exact false-positive class the noun-cooccurrence gate kills.
    assert result["passed"] is False
    assert result["fresh_relevance_count"] == 0


def test_phase_b_timed_out_when_fetch_returns_none():
    with patch.object(reddit, "fetch_feed", return_value=None):
        result = discover.validate_sub_freshness(
            "foo", user_vocab={"x"}, competitors=[],
        )
    assert result["timed_out"] is True
    assert result["passed"] is False


def test_phase_b_rejects_invalid_sub_name():
    result = discover.validate_sub_freshness(
        "not-valid-sub-name-too-long-and-has-dashes",
        user_vocab=set(), competitors=[],
    )
    assert result["error"] == "invalid_sub_name"


def test_phase_b_competitor_first_word_match():
    """'Drake Software' competitor should match a post mentioning just 'drake'."""
    resp = _new_json_response(
        ("Switching from drake", "looking for cheaper option", 5.0),
    )
    with patch.object(reddit, "fetch_feed", return_value=resp):
        result = discover.validate_sub_freshness(
            "accounting", user_vocab={"accounting"},
            competitors=["Drake Software"],
        )
    assert result["passed"] is True


# ─── v3: Confidence formula tests ──────────────────────────────────────


def test_confidence_full_signal_high():
    score = discover.compute_confidence(
        freq=4, freq_max=4, vocab_match=True,
        weighted_relevance=5.0, fresh_buyer_intent_count=15,
        is_noise=False,
    )
    assert score >= 90


def test_confidence_minimum_signal_low():
    score = discover.compute_confidence(
        freq=1, freq_max=4, vocab_match=False,
        weighted_relevance=0.6, fresh_buyer_intent_count=1,
        is_noise=False,
    )
    assert score < 50  # below threshold, will be dropped


def test_confidence_noise_penalty():
    """Same inputs, noise vs clean — clean should score higher."""
    clean = discover.compute_confidence(
        freq=2, freq_max=4, vocab_match=True,
        weighted_relevance=1.2, fresh_buyer_intent_count=5,
        is_noise=False,
    )
    noisy = discover.compute_confidence(
        freq=2, freq_max=4, vocab_match=True,
        weighted_relevance=1.2, fresh_buyer_intent_count=5,
        is_noise=True,
    )
    assert clean > noisy


def test_confidence_clamped_to_0_100():
    # Boundary cases
    assert 0 <= discover.compute_confidence(
        freq=0, freq_max=0, vocab_match=False,
        weighted_relevance=0.0, fresh_buyer_intent_count=0,
        is_noise=True,
    ) <= 100


# ─── v3: stale-only clarifier test ─────────────────────────────────────


def test_e2e_stale_only_clarifier_fires():
    """Phase A finds ≥3 candidates, Phase B kills them all on freshness.
    Should trigger the stale_only clarifier (not vertical)."""
    c = _conn()
    answers = {
        "what_offering": "automation tools",
        "who_to_reach": "founders",
        "pain_quote": "alternative to Zapier",
    }

    def fake_search(url, timeout=15):
        # Search returns matching threads
        return _reddit_search_response(
            ("automation", 50, 10, 30),
            ("nocode", 40, 8, 45),
            ("zapier", 30, 5, 60),
        )

    # All Phase B calls fail freshness check
    def stale_phase_b(sub_name, user_vocab, competitors, **kw):
        return {
            "fresh_post_count": 0, "fresh_buyer_intent_count": 0,
            "fresh_relevance_count": 0, "weighted_relevance": 0.0,
            "relevance_path": None, "recent_thread_url": None,
            "recent_thread_title": None, "recent_thread_age_h": None,
            "recent_thread_iso": None,
            "passed": False, "timed_out": False, "error": None,
        }

    with patch.object(reddit, "fetch_feed", side_effect=fake_search):
        with patch.object(enrich, "detect_providers", return_value={"dataforseo": False, "firecrawl": False}):
            with patch.object(discover, "validate_sub_freshness", side_effect=stale_phase_b):
                result = discover.discover_subs_for_profile(
                    answers, "https://example.com", c,
                    extra_competitors=["Zapier"],
                )

    assert result["needs_clarification"] is True
    assert result["clarifier_reason"] == "stale_only"
    assert "broaden" in result["clarifier_prompt"].lower()
    assert result["subs"] == []
    # All Phase A candidates went into dropped_subs with the freshness reason
    assert len(result["dropped_subs"]) >= 2
    assert all(d["reason"] == "no_fresh_buyer_activity" for d in result["dropped_subs"])


def test_e2e_recall_surfaces_gate_passer_with_low_confidence():
    """v3.2: a thin-but-real gate-passer (single thread, no vocab match) still
    SURFACES as a low-confidence candidate. The engine is the recall stage;
    precision is the skill-layer review's job. Only confidence < FLOOR drops."""
    c = _conn()
    answers = {
        "what_offering": "dispatch software for HVAC shops",
        "who_to_reach": "HVAC shop owners",
        "pain_quote": "Housecall Pro is too expensive, looking for a cheaper dispatch tool",
    }

    def fake_search(url, timeout=15):
        # Returns WhichCRM for every query so it gets cross-query confirmation
        return _reddit_search_response(("WhichCRM", 5, 1, 30))

    def thin_phase_b(sub_name, user_vocab, competitors, **kw):
        return {
            "fresh_post_count": 1, "fresh_buyer_intent_count": 1,
            "fresh_relevance_count": 1, "weighted_relevance": 0.6,
            "relevance_path": "noun",
            "recent_thread_url": f"https://reddit.com/r/{sub_name}/x/",
            "recent_thread_title": "best crm tool?", "recent_thread_age_h": 40.0,
            "recent_thread_iso": "2026-05-27 00:00 UTC",
            "recent_thread_reason": 'Buyer post 2d ago. Someone asking about a "crm" with buying intent ("best").',
            "passed": True, "timed_out": False, "error": None,
        }

    with patch.object(reddit, "fetch_feed", side_effect=fake_search):
        with patch.object(enrich, "detect_providers", return_value={"dataforseo": False, "firecrawl": False}):
            with patch.object(discover, "validate_sub_freshness", side_effect=thin_phase_b):
                result = discover.discover_subs_for_profile(
                    answers, "", c, extra_competitors=["Housecall Pro", "Jobber"])

    # The gate-passer surfaces (recall), carrying its reason, even at low conf.
    surfaced = {s["name"].lower() for s in result["subs"]}
    assert "whichcrm" in surfaced
    only = next(s for s in result["subs"] if s["name"].lower() == "whichcrm")
    assert only["confidence"] >= discover.CONFIDENCE_FLOOR
    assert only.get("recent_thread_reason")


# ─── v3.1: software_buyer_intent classifier matrix (architect's 15 cases) ──
# (title, body, competitors, expected_pass, expected_path)
# This is the regression guard for the "looking for interviewees for my
# podcast" false-positive class that shipped in v3.

_BUYER_INTENT_MATRIX = [
    # Dental software
    ("Cheaper alternative to Dentrix? quotes are insane", "",
     ["Dentrix"], True, "competitor"),
    ("Best PMS for a 3-chair practice, Open Dental vs Curve", "",
     ["Open Dental", "Curve"], True, "competitor"),
    ("My PMS is wrecking me this cycle, send chocolate", "",
     ["Dentrix"], False, None),
    # Podcast hosting (THE false-positive class)
    ("Looking for interviewees for my podcast", "",
     ["Libsyn"], False, None),
    ("Moving off Libsyn, their pricing doubled. recommendations?", "",
     ["Libsyn"], True, "competitor"),
    ("Anchor killed my back catalog, anyone else hate the new dashboard?", "",
     ["Anchor"], True, "competitor"),
    # Shopify subscriptions
    ("Recharge alternative? their billing keeps double-charging", "",
     ["Recharge"], True, "competitor"),
    ("Best coffee for my morning routine before I batch orders", "",
     ["Recharge"], False, None),
    ("Looking for a subscription app that does dunning well", "",
     ["Recharge"], True, "noun"),
    # Legal practice management
    ("Switching from Clio, what software handles trust accounting?", "",
     ["Clio"], True, "competitor"),
    ("Clio released v4 today", "",
     ["Clio"], False, None),
    # Restaurant scheduling
    ("Any tool to replace spreadsheet shift swaps? scheduling chaos", "",
     ["Homebase"], True, "noun"),
    # HVAC dispatch (negation)
    ("NOT looking for software, just venting about dispatch days", "",
     ["ServiceTitan"], False, None),
    ("ServiceTitan is too expensive, cheaper dispatch platform?", "",
     ["ServiceTitan"], True, "competitor"),
    # K8s observability
    ("Datadog bill hit $40k, migrate to cheaper stack?", "",
     ["Datadog"], True, "competitor"),
]


def _comp_tokens(competitors):
    s = set()
    for c in competitors:
        cl = c.strip().lower()
        s.add(cl)
        first = cl.split()[0]
        if len(first) >= 3:
            s.add(first)
    return s


def test_software_buyer_intent_matrix():
    """The architect's 15-case matrix. Asserts BOTH verdict and path so a
    regression that gets the right verdict via the wrong route is caught."""
    failures = []
    for title, body, competitors, exp_pass, exp_path in _BUYER_INTENT_MATRIX:
        passed, path, weight = discover.software_buyer_intent(
            title, body, _comp_tokens(competitors))
        if passed != exp_pass or (exp_pass and path != exp_path):
            failures.append(
                f"  {title[:50]!r}: got (pass={passed}, path={path}), "
                f"expected (pass={exp_pass}, path={exp_path})")
    assert not failures, "matrix failures:\n" + "\n".join(failures)


def test_software_buyer_intent_negation_survives_real_buyer():
    """'can't find a good alternative' is a REAL buyer (negation guard must
    not over-trigger). 'can' is not in negation set; 'find' is not intent."""
    passed, path, _ = discover.software_buyer_intent(
        "can't find a good alternative tool for invoicing", "",
        _comp_tokens([]))
    assert passed is True
    assert path == "noun"


def test_software_buyer_intent_anchor_word_boundary():
    """'anchor' as a common word (not the brand) must not trigger competitor
    path when the brand isn't actually named as a standalone token."""
    # "anchored" should NOT match brand "anchor" (word-boundary)
    passed, path, _ = discover.software_buyer_intent(
        "my boat is anchored offshore", "looking for nice views",
        _comp_tokens(["Anchor"]))
    assert passed is False


def test_competitor_common_first_word_not_promoted():
    """v3.1 QA bug: competitor 'When I Work' must NOT promote 'when' to a
    standalone brand token (it matched every 'when' in unrelated threads)."""
    toks = discover._build_competitor_tokens(["When I Work", "Homebase", "Deputy"])
    assert "when" not in toks            # common first word dropped
    assert "when i work" in toks         # full phrase kept
    assert "homebase" in toks
    # The exact false-positive thread must now REJECT
    passed, path, _ = discover.software_buyer_intent(
        "as managers what do you do when ur employees said I work to live", "",
        toks)
    assert passed is False


def test_distinctive_first_word_still_promoted():
    """'Drake Software' -> 'drake' is distinctive (len>=5, not common), kept."""
    toks = discover._build_competitor_tokens(["Drake Software"])
    assert "drake" in toks
    passed, path, _ = discover.software_buyer_intent(
        "switching from drake, recommendations?", "", toks)
    assert passed is True
    assert path == "competitor"


def test_generic_billing_noun_does_not_leak():
    """v3.1 QA bug: 'Medical billing vs Coding for Analytics' must REJECT.
    'billing' was removed from the product-noun set (domain-overlap word)."""
    passed, path, _ = discover.software_buyer_intent(
        "Medical billing vs Coding for Analytics", "",
        _comp_tokens(["ChiroTouch", "Jane App"]))
    assert passed is False


# ─── v3.2: reason-string truthfulness ──────────────────────────────────


def test_build_reason_only_names_fired_signals():
    """Hallucination tripwire: every brand/noun/token quoted in the reason
    string must actually appear in the source thread (case-insensitive)."""
    cases = [
        ("Best tool for document parsing?", "", []),
        ("Buzzsprout vs Acast vs Substack oh my", "", ["Buzzsprout", "Acast"]),
        ("Has anyone monetized Substack through Reddit?", "", ["Substack"]),
        ("Fed up with Drake Software pricing", "", ["Drake Software"]),
        ("Anyone switching CRM platforms this year?", "", []),
        ("Moving off Acast, suggestions?", "", ["Acast"]),
    ]
    for title, body, comps in cases:
        m = discover.software_buyer_intent(title, body, _comp_tokens(comps))
        reason = discover.build_reason(m, age_h=24.0)
        if not m.passed:
            assert reason is None
            continue
        assert reason is not None
        blob = (title + " " + body).lower()
        # Any quoted token in the reason must be a real substring of the thread
        import re as _re
        for quoted in _re.findall(r'"([^"]+)"', reason):
            assert quoted.lower() in blob, (
                f"reason quoted {quoted!r} not in thread {title!r}: {reason!r}")
        # Named competitor (if any) must be real
        if m.competitor:
            assert m.competitor.lower() in blob
        # No em dashes in user-facing reason
        assert "—" not in reason


def test_build_reason_none_when_not_passed():
    m = discover.software_buyer_intent(
        "Looking for interviewees for my podcast", "", _comp_tokens([]))
    assert m.passed is False
    assert discover.build_reason(m, age_h=10.0) is None


def test_build_reason_competitor_question_no_fake_intent():
    """Competitor + question marker must NOT fabricate an intent token."""
    m = discover.software_buyer_intent(
        "Has anyone monetized Substack through Reddit?", "", _comp_tokens(["Substack"]))
    assert m.passed is True
    assert m.path == "competitor"
    assert m.marker == "question"
    assert m.intent_token is None
    reason = discover.build_reason(m, age_h=24.0)
    assert "buying question" in reason


def test_buyer_intent_match_tuple_unpacks():
    """Backward-compat: BuyerIntentMatch unpacks to (passed, path, weight)."""
    m = discover.software_buyer_intent("Best tool for parsing", "", _comp_tokens([]))
    passed, path, weight = m
    assert passed is True and path == "noun" and weight == 0.6


def test_fresh_window_hours_threads_through_to_phase_b():
    """The 'broaden' clarifier path passes --fresh-window-hours; verify the
    window reaches validate_sub_freshness (regression guard for the broken
    broaden path the code review caught)."""
    c = _conn()
    answers = {
        "what_offering": "dispatch software for HVAC shops",
        "who_to_reach": "HVAC shop owners",
        "pain_quote": "Housecall Pro is too expensive, looking for a cheaper dispatch tool",
    }
    captured = {}

    def capture_window(sub_name, user_vocab, competitors, **kw):
        captured["window_hours"] = kw.get("window_hours")
        return {
            "fresh_post_count": 0, "fresh_buyer_intent_count": 0,
            "fresh_relevance_count": 0, "weighted_relevance": 0.0,
            "relevance_path": None, "recent_thread_url": None,
            "recent_thread_title": None, "recent_thread_age_h": None,
            "recent_thread_iso": None, "recent_thread_reason": None,
            "passed": False, "timed_out": False, "error": None,
        }

    with patch.object(reddit, "fetch_feed",
                      side_effect=lambda u, timeout=15: _reddit_search_response(("HVAC", 5, 2, 5))):
        with patch.object(enrich, "detect_providers", return_value={"dataforseo": False, "firecrawl": False}):
            with patch.object(discover, "validate_sub_freshness", side_effect=capture_window):
                discover.discover_subs_for_profile(
                    answers, "", c, extra_competitors=["Housecall Pro", "Jobber"],
                    fresh_window_hours=720)
    assert captured.get("window_hours") == 720


def test_future_dated_post_not_counted_fresh():
    """Clock-skew guard: a post dated in the future must not count as fresh."""
    future = int(time.time()) + 10 * 86400  # 10 days ahead
    resp = [
        {"id": "t3_future", "subreddit": "test", "title": "alternative to Clio tool?",
         "body": "switching", "created_utc": future, "url": "/r/test/comments/x/"},
    ]
    with patch.object(reddit, "fetch_feed", return_value=resp):
        result = discover.validate_sub_freshness(
            "LawFirm", user_vocab=set(), competitors=["Clio"])
    assert result["fresh_post_count"] == 0
    assert result["passed"] is False


def test_evidence_has_absolute_timestamp():
    """Every surfaced evidence thread must carry an absolute UTC timestamp."""
    resp = _new_json_response(
        ("Switching from Clio, need software for trust accounting", "", 6.0),
    )
    with patch.object(reddit, "fetch_feed", return_value=resp):
        result = discover.validate_sub_freshness(
            "LawFirm", user_vocab=set(), competitors=["Clio"])
    assert result["passed"] is True
    assert result["recent_thread_iso"] is not None
    assert "UTC" in result["recent_thread_iso"]
    assert result["recent_thread_created_utc"] is not None


# ─── Subreddit-directory search (the discovery recall fix) ─────────────


def _sr_feed(*subs):
    """Build a /subreddits/search.rss Atom root, one <entry> per subreddit."""
    import xml.etree.ElementTree as ET
    ns = "http://www.w3.org/2005/Atom"
    feed = ET.Element(f"{{{ns}}}feed")
    for name in subs:
        e = ET.SubElement(feed, f"{{{ns}}}entry")
        ET.SubElement(e, f"{{{ns}}}title").text = name
        ET.SubElement(e, f"{{{ns}}}link").set("href", f"https://www.reddit.com/r/{name}/")
    return feed


def test_search_subreddits_extracts_sub_from_link():
    root = _sr_feed("projectmanagement", "agency", "consulting")
    with patch.object(reddit, "fetch_xml_resilient", return_value=root):
        hits = discover.search_subreddits("project management", sleep_between=0)
    assert [h["sub"] for h in hits] == ["projectmanagement", "agency", "consulting"]
    assert hits[0]["source"] == "subreddit_search"
    assert hits[0]["rank_pos"] == 0


def test_search_subreddits_unreachable_returns_empty():
    with patch.object(reddit, "fetch_xml_resilient", return_value=None):
        assert discover.search_subreddits("x", sleep_between=0) == []


def test_derive_sub_queries_prioritizes_competitors_industry_offering():
    answers = {
        "what_offering": "project management for agencies and services firms",
        "who_to_reach": "ops managers and delivery leads at agencies and consulting firms",
        "pain_quote": "x",
    }
    q = discover.derive_sub_queries(answers, ["Asana", "Monday.com"], None)
    assert "asana" in q and "monday" in q           # competitor communities
    assert "agencies" in q and "consulting" in q     # industry nouns (after "at")
    assert "leads" not in q and "managers" not in q  # role words excluded


def test_rank_subs_directory_surfaces_community_with_no_threads():
    vocab = {"project", "management", "agency"}
    hits = [{"sub": "projectmanagement", "display_title": "Project Management",
             "rank_pos": 0, "source_query": "project management",
             "source": "subreddit_search"}]
    ranked = discover.rank_subs([], user_vocab=vocab, directory_hits=hits)
    top = next((r for r in ranked if r["name"] == "projectmanagement"), None)
    assert top is not None                 # surfaced despite zero threads
    assert top["directory_match"] is True
    assert top["vocab_match"] is True


def test_rank_subs_directory_drops_offtopic_noise():
    # No vocab/competitor signal -> dropped (skill review never even sees it).
    hits = [{"sub": "labrador", "display_title": "The bestest dogs", "rank_pos": 3,
             "source_query": "professional services", "source": "subreddit_search"}]
    ranked = discover.rank_subs([], user_vocab={"project", "management"}, directory_hits=hits)
    assert all(r["name"] != "labrador" for r in ranked)


def test_rank_subs_directory_marks_competitor_community():
    hits = [{"sub": "Asana", "display_title": "Asana", "rank_pos": 0,
             "source_query": "asana", "source": "subreddit_search"}]
    ranked = discover.rank_subs([], user_vocab=set(), comp_tokens={"asana"},
                                directory_hits=hits)
    top = next((r for r in ranked if r["name"].lower() == "asana"), None)
    assert top is not None and top["competitor_match"] is True


def test_rank_subs_directory_boosts_existing_thread_sub():
    # A sub found via BOTH threads and the directory gets the directory_match flag
    # and both sources (cross-source confirmation).
    threads = [
        {"sub": "projectmanagement", "score": 5, "num_comments": 1,
         "created_utc": NOW - 86400, "source_query": "q1", "source": "reddit_native"},
        {"sub": "projectmanagement", "score": 5, "num_comments": 1,
         "created_utc": NOW - 86400, "source_query": "q2", "source": "reddit_native"},
    ]
    hits = [{"sub": "projectmanagement", "display_title": "Project Management",
             "rank_pos": 0, "source_query": "project management",
             "source": "subreddit_search"}]
    ranked = discover.rank_subs(threads, user_vocab={"project", "management"},
                                directory_hits=hits)
    pm = next((r for r in ranked if r["name"] == "projectmanagement"), None)
    assert pm is not None and pm["directory_match"] is True
    assert "subreddit_search" in pm["sources"] and "reddit_native" in pm["sources"]


def test_comp_hit_dotted_brand_matches_community_not_weekday():
    # Monday.com: the dotcom community should match; the bare weekday sub must not.
    hits = [
        {"sub": "mondaydotcom", "display_title": "monday.com", "rank_pos": 0,
         "source_query": "monday", "source": "subreddit_search"},
        {"sub": "monday", "display_title": "Monday", "rank_pos": 1,
         "source_query": "monday", "source": "subreddit_search"},
    ]
    ranked = discover.rank_subs([], user_vocab=set(), comp_tokens={"monday.com"},
                                directory_hits=hits)
    by = {r["name"].lower(): r for r in ranked}
    assert "mondaydotcom" in by and by["mondaydotcom"]["competitor_match"] is True
    assert "monday" not in by  # weekday sub: no comp_hit, no vocab -> dropped


def test_comp_hit_no_overmatch_on_short_token():
    # A 2-char brand must not suffix-match an unrelated sub (r/gousers).
    hits = [{"sub": "gousers", "display_title": "Go Users", "rank_pos": 0,
             "source_query": "go", "source": "subreddit_search"}]
    ranked = discover.rank_subs([], user_vocab=set(), comp_tokens={"go"},
                                directory_hits=hits)
    assert all(r["name"] != "gousers" for r in ranked)


def test_audience_industry_nouns_skips_prepositions():
    nouns = discover._audience_industry_nouns(
        "ops leads at agencies and consulting firms serving healthcare")
    assert "agencies" in nouns and "consulting" in nouns
    assert "serving" not in nouns


def test_skip_validation_relevance_differentiates():
    # The bug: every directory community got floored to the same number (72).
    # Now same-tier subs with different rank scores get different relevance.
    shown = [
        {"name": "projectmanagement", "score": 14.5, "vocab_match": True, "competitor_match": False, "directory_match": True, "freq": 2},
        {"name": "Asana", "score": 14.0, "vocab_match": False, "competitor_match": True, "directory_match": True, "freq": 1},
        {"name": "consulting", "score": 11.5, "vocab_match": True, "competitor_match": False, "directory_match": True, "freq": 1},
        {"name": "projectmanagers", "score": 11.2, "vocab_match": True, "competitor_match": False, "directory_match": True, "freq": 1},
    ]
    discover._assign_relevance(shown)
    rels = [c["relevance"] for c in shown]
    assert len(set(rels)) > 1                       # NOT all identical
    assert all(74 <= r <= 96 for r in rels)         # tier-3 band, honest sub-100 ceiling
    pm = next(c for c in shown if c["name"] == "projectmanagement")
    pmgrs = next(c for c in shown if c["name"] == "projectmanagers")
    assert pm["relevance"] > pmgrs["relevance"]     # higher rank score -> higher relevance


def test_skip_validation_directory_outranks_thread_noise():
    # A tier-3 directory community must read higher than a tier-2 thread-host sub
    # even when the thread sub has a higher raw score.
    shown = [
        {"name": "projectmanagement", "score": 12.0, "vocab_match": True, "competitor_match": False, "directory_match": True, "freq": 1},
        {"name": "SideProject", "score": 18.0, "vocab_match": True, "competitor_match": True, "directory_match": False, "freq": 3},
    ]
    discover._assign_relevance(shown)
    pm = next(c for c in shown if c["name"] == "projectmanagement")
    sp = next(c for c in shown if c["name"] == "SideProject")
    assert pm["relevance"] > sp["relevance"]


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
