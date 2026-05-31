"""Per-mode fetch routing through cmd_fetch_score.

Pins the core fix: default mode keeps the /new delta path untouched, every other
mode fetches via reddit.fetch_search, resurrect age-bands to 6-18 months and never
falls back to /new, search modes never advance the /new cursor watermark, and the
resurrect weights override stops freshness decay from zeroing old threads.

Mocks the network boundary (fetch_search / fetch_delta); no real Reddit calls.
"""
import io
import json
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subscope import cli  # noqa: E402
from subscope.lib import reddit, store, score  # noqa: E402

NOW = 1780000000
REPO_CONFIG = Path(__file__).resolve().parent.parent.parent / "config"


@pytest.fixture
def _cfg(tmp_path, monkeypatch):
    cfgd = tmp_path / "cfg"
    cfgd.mkdir()
    (cfgd / "subreddits.yml").write_text(yaml.safe_dump({"subreddits": [
        {"name": "msp", "tier": 1, "bucket": "operator", "weight": 1.0},
    ]}))
    (cfgd / "keywords.yml").write_text(yaml.safe_dump({
        "shared": ["alternative", "switching from", "rmm", "psa"],
        "operator": ["canceling", "renewal", "price hike"],
        "builder": ["build vs buy"],
    }))
    # Use the REAL base + resurrect weights so the shallow-merge contract is
    # exercised exactly as in production.
    shutil.copy(REPO_CONFIG / "weights.yml", cfgd / "weights.yml")
    shutil.copy(REPO_CONFIG / "weights-resurrect.yml", cfgd / "weights-resurrect.yml")
    (cfgd / "brand-anchor.yml").write_text(yaml.safe_dump(
        {"brand_anchor": ["ConnectWise", "Datto", "NinjaOne", "Atera"]}))
    monkeypatch.setenv("SUBSCOPE_CONFIG", str(cfgd))
    monkeypatch.setenv("SUBSCOPE_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "CONFIG_DIR", cfgd)
    store.bootstrap()
    reddit.reset_fetch_stats()
    reddit._last_request_at = 0.0
    monkeypatch.setattr(reddit, "_sleep", lambda s: None)
    monkeypatch.setattr(score, "now_utc", lambda: NOW)
    return cfgd


def _post(pid, title, body="", age_h=2.0, **over):
    d = {
        "id": pid, "subreddit": "msp", "title": title,
        "url": f"https://reddit.com/r/msp/comments/{pid}/x/",
        "canonical_url": f"https://reddit.com/comments/{pid}/",
        "author": "op", "created_utc": int(NOW - age_h * 3600),
        "score": 0, "num_comments": 0, "body": body, "upvote_ratio": None,
        "removed": False, "locked": False, "over_18": False, "is_crosspost": False,
    }
    d.update(over)
    return d


def _run(monkeypatch, mode, *, search_posts=None, delta_posts=None,
         search_none=False, **kw):
    calls = {"search": 0, "delta": 0}

    def _fake_search(sub, query, *, sort="new", t=None, limit=25,
                     restrict_sr=True, timeout=12):
        calls["search"] += 1
        return None if search_none else list(search_posts or [])

    def _fake_delta(sub, cur, max_limit=50):
        calls["delta"] += 1
        return list(delta_posts or [])

    monkeypatch.setattr(reddit, "fetch_search", _fake_search)
    monkeypatch.setattr(reddit, "fetch_delta", _fake_delta)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.cmd_fetch_score(mode=mode, no_slack=True, no_enrich=True, **kw)
    return json.loads(buf.getvalue().strip()), calls


# ─── default mode is UNCHANGED (the #1 regression guard) ───────────────

def test_default_mode_uses_delta_never_search(_cfg, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("fetch_search must never be called in default mode")
    monkeypatch.setattr(reddit, "fetch_search", _boom)
    monkeypatch.setattr(reddit, "fetch_delta",
                        lambda sub, cur, max_limit=50: [
                            _post("d1", "ConnectWise renewal doubled, alternatives?")])
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.cmd_fetch_score(mode="default", no_slack=True, no_enrich=True, candidates=True)
    out = json.loads(buf.getvalue().strip())
    assert out["mode"] == "default"
    assert any(c["id"] == "d1" for c in out.get("candidates", []))


def test_default_mode_advances_cursor(_cfg, monkeypatch):
    monkeypatch.setattr(reddit, "fetch_search", lambda *a, **k: None)
    monkeypatch.setattr(reddit, "fetch_delta",
                        lambda sub, cur, max_limit=50: [_post("newest", "x")])
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.cmd_fetch_score(mode="default", no_slack=True, no_enrich=True)
    with store.connect() as conn:
        assert store.get_sub(conn, "msp")["last_cursor"] == "newest"


# ─── pattern modes route through search ────────────────────────────────

def test_churn_mode_uses_search(_cfg, monkeypatch):
    out, calls = _run(monkeypatch, "churn", candidates=True,
                      search_posts=[_post("s1", "Canceling ConnectWise, moving to NinjaOne")])
    assert calls["search"] >= 1
    assert calls["delta"] == 0
    assert out["mode"] == "churn"
    assert any(c["id"] == "s1" for c in out.get("candidates", []))


def test_search_mode_does_not_advance_cursor(_cfg, monkeypatch):
    with store.connect() as conn:
        store.upsert_subreddit(conn, {"name": "msp", "tier": 1,
                                      "bucket": "operator", "weight": 1.0})
        store.update_cursor(conn, "msp", "ORIGINAL")
    _run(monkeypatch, "churn", candidates=True,
         search_posts=[_post("s2", "Canceling Datto")])
    with store.connect() as conn:
        # Search results must NOT corrupt the default /new delta watermark.
        assert store.get_sub(conn, "msp")["last_cursor"] == "ORIGINAL"


def test_search_unreachable_falls_back_to_new_for_pattern_mode(_cfg, monkeypatch):
    out, calls = _run(monkeypatch, "churn", candidates=True, search_none=True,
                      delta_posts=[_post("f1", "Switching from ConnectWise")])
    assert calls["search"] >= 1
    assert calls["delta"] >= 1  # fell back to /new on unreachable search
    assert any(c["id"] == "f1" for c in out.get("candidates", []))


# ─── resurrect: age band + no /new fallback ────────────────────────────

def test_resurrect_age_band_filters_young_and_ancient(_cfg, monkeypatch):
    young = _post("young", "ConnectWise alternative", age_h=1)          # < 6mo floor
    inband = _post("mid", "ConnectWise alternative thread", age_h=6480)  # 270d, in band
    ancient = _post("old", "ConnectWise alternative", age_h=17520)       # 730d, > 18mo
    out, calls = _run(monkeypatch, "resurrect", candidates=True,
                      search_posts=[young, inband, ancient])
    ids = {c["id"] for c in out.get("candidates", [])}
    assert "mid" in ids
    assert "young" not in ids and "old" not in ids


def test_resurrect_unreachable_does_not_fall_back(_cfg, monkeypatch):
    out, calls = _run(monkeypatch, "resurrect", candidates=True, search_none=True,
                      delta_posts=[_post("nope", "should never appear")])
    assert calls["search"] >= 1
    assert calls["delta"] == 0          # resurrect never reads /new
    assert out["fetched"] == 0


# ─── resurrect weights override: freshness no longer zeros old threads ──

def test_resurrect_freshness_override_scores_old_thread():
    base = yaml.safe_load((REPO_CONFIG / "weights.yml").read_text())
    over = yaml.safe_load((REPO_CONFIG / "weights-resurrect.yml").read_text())
    merged = {**base, **over}
    sub = {"name": "msp", "tier": 1, "bucket": "operator", "weight": 1.0}
    # A 270-day-old post with no keyword/intent/brand signal: ONLY freshness can
    # produce a nonzero score. Proves the override works and the merge kept scoring.
    post = _post("r", "an old thread with nothing matching", "", age_h=6480)
    no_kw = ["zzz_nonmatching_kw"]
    s_base = score.compute_score(post, sub, [], base, no_kw, now=NOW)
    s_res = score.compute_score(post, sub, [], merged, no_kw, now=NOW)
    assert s_base == 0.0     # default freshness (zero at 30h) zeros a 270d post
    assert s_res > 0.0       # resurrect freshness window keeps it alive


# ─── resurrect 2nd-call rate-limit guard (only when year page is full) ──

def _run_resurrect_split(monkeypatch, year_posts, all_posts):
    """Mock fetch_search to return distinct pages for t=year vs t=all; track t."""
    seen = {"t": []}

    def _fs(sub, query, *, sort="new", t=None, limit=25, restrict_sr=True, timeout=12):
        seen["t"].append(t)
        return list(all_posts) if t == "all" else list(year_posts)

    monkeypatch.setattr(reddit, "fetch_search", _fs)
    monkeypatch.setattr(reddit, "fetch_delta",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no /new in resurrect")))
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.cmd_fetch_score(mode="resurrect", no_slack=True, no_enrich=True,
                            candidates=True, limit_per_sub=25)
    return json.loads(buf.getvalue().strip()), seen


def test_resurrect_widens_on_full_year_page(_cfg, monkeypatch):
    year = [_post(f"y{i}", "x", age_h=1) for i in range(25)]   # full page, all sub-6mo
    older = [_post("oldwin", "ConnectWise alternative", age_h=6480)]  # in-band 270d
    out, seen = _run_resurrect_split(monkeypatch, year, older)
    assert "all" in seen["t"]            # full year page triggers the t=all widen
    assert any(c["id"] == "oldwin" for c in out.get("candidates", []))


def test_resurrect_no_widen_on_thin_year_page(_cfg, monkeypatch):
    thin = [_post(f"y{i}", "x", age_h=1) for i in range(3)]    # thin page (< cap)
    out, seen = _run_resurrect_split(monkeypatch, thin, [])
    assert "all" not in seen["t"]        # thin page: skip the 2nd GET (rate guard)


def test_resurrect_window_override_drops_age_floor(_cfg, monkeypatch):
    # --window forces a recency window; the 6-month resurrect floor must drop so
    # recent posts are not all banded out (otherwise the combo returns nothing).
    recent = _post("r1", "ConnectWise pricing thread", age_h=100)  # ~4 days
    out, calls = _run(monkeypatch, "resurrect", search_posts=[recent],
                      candidates=True, window="month")
    assert any(c["id"] == "r1" for c in out.get("candidates", []))
