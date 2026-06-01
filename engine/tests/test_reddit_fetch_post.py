"""Tests for reddit.fetch_post: keyless single-submission fetch via
`/comments/<id>/.rss`.

fetch_post is the RSS replacement for the dead anonymous `.json` single-post
path (403 since 2026-05-29) that the /subscope-judge skill used. These tests are
fully offline: they mock fetch_xml_resilient (the dual-host transport seam) with
inline Atom roots, so no network and no sleeps. The transport itself (failover,
throttle, 429) is covered by test_reddit_fallback.py; here we pin the NEW
surfaces fetch_post adds: id extraction/validation order, and selecting the t3
submission entry by id-match (never by position).
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subscope.lib import reddit  # noqa: E402


# ─── Fixtures ─────────────────────────────────────────────────────────

def _submission_entry(pid="1tswzsc", sub="projectmanagement",
                      body="HubSpot is too expensive, any alternative?",
                      title="What PM feature saves your team the most time?"):
    content = (
        "&lt;!-- SC_OFF --&gt;&lt;div class=&quot;md&quot;&gt;&lt;p&gt;"
        + body +
        "&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt; &amp;#32; submitted by &amp;#32; "
        "&lt;a href=&quot;https://www.reddit.com/user/x&quot;&gt; /u/x &lt;/a&gt;"
    ) if body else ""
    return (
        "<entry>"
        "<author><name>/u/x</name></author>"
        f'<category term="{sub}" label="r/{sub}"/>'
        f'<content type="html">{content}</content>'
        f"<id>t3_{pid}</id>"
        f'<link href="https://www.reddit.com/r/{sub}/comments/{pid}/t/" />'
        "<published>2026-05-29T10:14:46+00:00</published>"
        f"<title>{title}</title>"
        "</entry>"
    )


def _comment_entry(pid="1tswzsc", cid="op426qu", sub="projectmanagement"):
    return (
        "<entry>"
        "<author><name>/u/commenter</name></author>"
        f'<category term="{sub}" label="r/{sub}"/>'
        '<content type="html">a reply</content>'
        f"<id>t1_{cid}</id>"
        f'<link href="https://www.reddit.com/r/{sub}/comments/{pid}/t/{cid}/" />'
        "<published>2026-05-29T11:00:00+00:00</published>"
        "<title>comment</title>"
        "</entry>"
    )


def _feed(*entries):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            '<title>post : reddit.com</title>'
            + "".join(entries) + "</feed>")


def _spy(monkeypatch, feed_xml):
    """Patch fetch_xml_resilient to record the path it was asked for and return
    a parsed root from feed_xml (or None to simulate unreachable)."""
    calls: list[str] = []

    def fake(path, timeout=15):
        calls.append(path)
        return ET.fromstring(feed_xml) if feed_xml is not None else None

    monkeypatch.setattr(reddit, "fetch_xml_resilient", fake)
    return calls


# ─── id extraction / validation (no network on bad input) ─────────────

def test_bare_id_builds_comments_rss_path(monkeypatch):
    calls = _spy(monkeypatch, _feed(_submission_entry()))
    post = reddit.fetch_post("1tswzsc")
    assert post is not None and post["id"] == "1tswzsc"
    assert calls == ["/comments/1tswzsc/.rss"]


def test_full_url_extracts_id(monkeypatch):
    calls = _spy(monkeypatch, _feed(_submission_entry()))
    post = reddit.fetch_post(
        "https://www.reddit.com/r/projectmanagement/comments/1tswzsc/what_pm/")
    assert post is not None and post["id"] == "1tswzsc"
    assert calls == ["/comments/1tswzsc/.rss"]


def test_trailing_slash_and_query_string(monkeypatch):
    calls = _spy(monkeypatch, _feed(_submission_entry()))
    reddit.fetch_post("https://reddit.com/comments/1tswzsc/?utm_source=share")
    assert calls == ["/comments/1tswzsc/.rss"]


@pytest.mark.parametrize("host", ["old.reddit.com", "np.reddit.com", "m.reddit.com"])
def test_alt_hosts_normalize(monkeypatch, host):
    calls = _spy(monkeypatch, _feed(_submission_entry()))
    reddit.fetch_post(f"https://{host}/r/projectmanagement/comments/1tswzsc/t/")
    assert calls == ["/comments/1tswzsc/.rss"]


def test_strips_t3_prefix(monkeypatch):
    calls = _spy(monkeypatch, _feed(_submission_entry()))
    reddit.fetch_post("t3_1tswzsc")
    assert calls == ["/comments/1tswzsc/.rss"]


def test_uppercase_id_lowercased(monkeypatch):
    calls = _spy(monkeypatch, _feed(_submission_entry()))
    post = reddit.fetch_post("1TSWZSC")
    assert calls == ["/comments/1tswzsc/.rss"]
    assert post is not None and post["id"] == "1tswzsc"


@pytest.mark.parametrize("bad", [
    "abc/../../user/x", "abc/.json", "abc?x=1", "x/.rss",
    "", "   ", "https://reddit.com/", "not a url", "https://reddit.com/r/saas/",
])
def test_bad_input_rejected_before_network(monkeypatch, bad):
    calls = _spy(monkeypatch, _feed(_submission_entry()))
    assert reddit.fetch_post(bad) is None
    assert calls == []  # validation fired before any fetch_xml_resilient call


def test_none_input_no_raise(monkeypatch):
    calls = _spy(monkeypatch, _feed(_submission_entry()))
    assert reddit.fetch_post(None) is None  # type: ignore[arg-type]
    assert calls == []


# ─── entry selection (the new "pick the submission" surface) ──────────

def test_selects_submission_not_leading_comment(monkeypatch):
    # Comment entry FIRST, submission second: must not return entries[0].
    _spy(monkeypatch, _feed(_comment_entry(), _submission_entry()))
    post = reddit.fetch_post("1tswzsc")
    assert post is not None
    assert post["id"] == "1tswzsc"
    assert post["canonical_url"] == "https://reddit.com/comments/1tswzsc/"


def test_comment_only_feed_returns_none(monkeypatch):
    _spy(monkeypatch, _feed(_comment_entry()))
    assert reddit.fetch_post("1tswzsc") is None


def test_empty_feed_returns_none(monkeypatch):
    _spy(monkeypatch, _feed())
    assert reddit.fetch_post("1tswzsc") is None


def test_comment_deeplink_returns_parent_post(monkeypatch):
    calls = _spy(monkeypatch, _feed(_submission_entry()))
    post = reddit.fetch_post(
        "https://www.reddit.com/r/projectmanagement/comments/1tswzsc/t/op426qu/")
    assert calls == ["/comments/1tswzsc/.rss"]
    assert post is not None and post["id"] == "1tswzsc"


# ─── content shape / link posts / unreachable ─────────────────────────

def test_link_post_empty_body_still_returns(monkeypatch):
    _spy(monkeypatch, _feed(_submission_entry(body="")))
    post = reddit.fetch_post("1tswzsc")
    assert post is not None
    assert post["body"] == ""
    assert post["title"] and post["subreddit"] == "projectmanagement"


def test_return_shape_matches_parse_atom_entry_keys(monkeypatch):
    _spy(monkeypatch, _feed(_submission_entry()))
    post = reddit.fetch_post("1tswzsc")
    expected = {
        "id", "subreddit", "title", "url", "canonical_url", "author",
        "created_utc", "score", "num_comments", "body", "upvote_ratio",
        "removed", "locked", "over_18", "is_crosspost",
    }
    assert set(post.keys()) == expected


def test_none_when_unreachable(monkeypatch):
    _spy(monkeypatch, None)
    assert reddit.fetch_post("1tswzsc") is None


def test_uses_resilient_failover_path(monkeypatch):
    """fetch_post must route through fetch_xml_resilient (so it inherits the
    www->old.reddit 403 failover), not a fresh single-host GET. Drive the real
    fetch_xml_resilient with a mocked attempt seam: www fails, old succeeds."""
    reddit.reset_fetch_stats()
    root = ET.fromstring(_feed(_submission_entry()))

    def fake_attempt(url, timeout=15):
        return ("failed", None) if "www.reddit.com" in url else ("ok", root)

    monkeypatch.setattr(reddit, "_fetch_xml_attempt", fake_attempt)
    post = reddit.fetch_post("1tswzsc")
    assert post is not None and post["id"] == "1tswzsc"
    assert reddit.get_fetch_stats()["fallback_used"] == 1


def test_rate_limited_returns_none_and_flags(monkeypatch):
    """A 429 mid-fetch -> fetch_xml_resilient None + drained flag set, so the
    skill can show a rate-limit message rather than a hard 'unreachable'."""
    reddit.reset_fetch_stats()
    monkeypatch.setattr(reddit, "_fetch_xml_attempt",
                        lambda url, timeout=15: ("rate_limited", None))
    assert reddit.fetch_post("1tswzsc") is None
    assert reddit.is_rate_limited() is True
