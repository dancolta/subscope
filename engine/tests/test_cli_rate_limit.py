"""CLI rate-limit discipline: partial results + three-state status.

Exercises cmd_fetch_score end to end with NO network: reddit.fetch_delta and the
rate-limit signals are mocked. Verifies that when Reddit drains the token bucket
mid-run, the CLI stops bursting, returns what it already fetched, and reports
status="rate_limited" (not "blocked", not a misleading empty day).
"""
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subscope import cli  # noqa: E402
from subscope.lib import reddit, store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_xdg(tmp_path, monkeypatch):
    # Empty config dir -> CONFIG_DIR (resolved at import) keeps repo-local
    # config/, which has real subs. We only isolate the DATA (DB) dir here.
    monkeypatch.setenv("SUBSCOPE_CONFIG", str(tmp_path / "cfg"))
    monkeypatch.setenv("SUBSCOPE_DATA", str(tmp_path / "data"))
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    store.bootstrap()
    reddit.reset_fetch_stats()
    reddit._last_request_at = 0.0
    # Never sleep for real in CLI tests.
    monkeypatch.setattr(reddit, "_sleep", lambda s: None)


def _run_fetch_score(**kwargs) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.cmd_fetch_score(no_slack=True, **kwargs)
    # The payload is the last JSON object printed.
    out = buf.getvalue().strip()
    return json.loads(out)


def _make_posts(sub: str, n: int = 2):
    base = 1780000000
    return [
        {
            "id": f"{sub}_{i}", "subreddit": sub,
            "title": "HubSpot is too expensive, alternative to it?",
            "url": f"https://www.reddit.com/r/{sub}/comments/{sub}_{i}/x/",
            "canonical_url": f"https://reddit.com/comments/{sub}_{i}/",
            "author": "op_user", "created_utc": base - 600,
            "score": 0, "num_comments": 0, "body": "paying too much for HubSpot",
            "upvote_ratio": None, "removed": False, "locked": False,
            "over_18": False, "is_crosspost": False,
        }
        for i in range(n)
    ]


def test_status_ok_when_no_rate_limit(monkeypatch):
    """All feeds reachable, no 429 -> status ok."""
    monkeypatch.setattr(reddit, "fetch_delta", lambda *a, **k: [])
    monkeypatch.setattr(reddit, "is_rate_limited", lambda: False)
    # cmd_fetch_score resets stats internally, so control the FINAL read.
    monkeypatch.setattr(reddit, "get_fetch_stats",
                        lambda: {"ok": 5, "failed": 0, "rate_limited": 0})
    payload = _run_fetch_score(limit_per_sub=3, daily_cap=3, no_cool=True)
    assert payload["status"] == "ok"
    assert payload["subs_skipped_rate_limit"] == 0
    assert "fetch_rate_limited" not in payload["dropped_counts"]
    assert "fetch_blocked" not in payload["dropped_counts"]


def test_status_blocked_when_all_feeds_fail_non_429(monkeypatch):
    """Every feed GET failed for a non-429 reason, nothing fetched -> blocked."""
    monkeypatch.setattr(reddit, "fetch_delta", lambda *a, **k: [])
    monkeypatch.setattr(reddit, "is_rate_limited", lambda: False)
    monkeypatch.setattr(reddit, "get_fetch_stats",
                        lambda: {"ok": 0, "failed": 18, "rate_limited": 0})
    payload = _run_fetch_score(limit_per_sub=3, daily_cap=3, no_cool=True)
    assert payload["status"] == "blocked"
    assert payload["dropped_counts"]["fetch_blocked"] == 18


def test_status_rate_limited_and_partial_results(monkeypatch):
    """Bucket drains part-way through the batched prefetch: the run returns the
    subs it did get, and reports rate_limited with a count of skipped subs.

    The prefetch is where the network work happens on the default path, so a
    short batch (not a broken loop) is what a drained bucket looks like now.
    """
    covered = ["smallbusiness"]

    def fake_prime(subs, **kwargs):
        # One sub covered before the bucket drained; the rest never fetched.
        return [str(s["name"]) for s in subs if str(s["name"]) not in covered]

    def fake_fetch_delta(sub, last_seen_id, max_limit=50):
        if sub not in covered:
            return []
        reddit._FETCH_STATS["ok"] += 1
        return _make_posts(sub, 2)

    monkeypatch.setattr(reddit, "prime_new_cache", fake_prime)
    monkeypatch.setattr(reddit, "fetch_delta", fake_fetch_delta)
    monkeypatch.setattr(reddit, "is_rate_limited", lambda: False)
    # author vet must not hit the network in this test.
    monkeypatch.setattr(reddit, "fetch_user_about", lambda u: None)
    monkeypatch.setattr(reddit, "fetch_user_recent_subs", lambda u, limit=100: {})

    payload = _run_fetch_score(limit_per_sub=3, daily_cap=5, no_cool=True)

    assert payload["status"] == "rate_limited"
    assert payload["subs_skipped_rate_limit"] >= 1
    assert payload["dropped_counts"]["fetch_rate_limited"] >= 1
    # Partial, not wholesale-zero: the one covered sub still produced output.
    assert payload["fetched"] == 2
    assert payload["subs_scanned"] == 1
    # fetch_rate_limited is a status marker, kept OUT of the "filtered" footer.
    assert "posts filtered before scoring" not in payload["inline_table"] or \
        "rate" not in payload["inline_table"].lower()


def test_per_sub_path_still_breaks_when_bucket_drains(monkeypatch):
    """Search modes fetch per sub (no shared feed), so the mid-loop rate-limit
    break must still fire there and return partial results."""
    fetched_subs = []

    def fake_search(sub, mode, strat, keywords, brands, limit):
        fetched_subs.append(sub["name"])
        reddit._FETCH_STATS["ok"] += 1
        return _make_posts(sub["name"], 2)

    monkeypatch.setattr(cli, "_fetch_mode_search", fake_search)
    monkeypatch.setattr(reddit, "is_rate_limited", lambda: len(fetched_subs) >= 1)
    monkeypatch.setattr(reddit, "fetch_user_about", lambda u: None)
    monkeypatch.setattr(reddit, "fetch_user_recent_subs", lambda u, limit=100: {})

    payload = _run_fetch_score(limit_per_sub=3, daily_cap=5, no_cool=True,
                               mode="churn")

    # Exactly one sub fetched before the loop broke.
    assert len(fetched_subs) == 1
    assert payload["status"] == "rate_limited"
    assert payload["subs_skipped_rate_limit"] >= 1


def test_rate_limited_takes_precedence_over_blocked(monkeypatch):
    """If some GETs 429'd AND some failed, the run is rate_limited (transient),
    never blocked (which would wrongly imply an edge ban)."""
    monkeypatch.setattr(reddit, "fetch_delta", lambda *a, **k: [])
    monkeypatch.setattr(reddit, "is_rate_limited", lambda: False)
    monkeypatch.setattr(reddit, "get_fetch_stats",
                        lambda: {"ok": 0, "failed": 3, "rate_limited": 2})
    payload = _run_fetch_score(limit_per_sub=3, daily_cap=3, no_cool=True)
    assert payload["status"] == "rate_limited"
    assert "fetch_blocked" not in payload["dropped_counts"]


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
