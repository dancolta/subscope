"""subscope CLI. Python-side does fetch + gate + score + SQLite.

Claude (chat session) is the outer orchestrator per stress-test outcome #1:
- Claude calls Playwright MCP for blog refresh (rarely; only on Dan's signal),
  then passes any new blog post via `blog ingest`.
- Claude invokes `fetch-score`; receives JSON of surfaces.
- Claude calls Notion MCP to sync the daily board.

This script does NOT call any MCP tools. Stdlib + pyyaml only.

Subcommands:
  setup          Bootstrap SQLite, seed configs from YAML
  fetch-score    Fetch deltas, gate, score, mark surfaced, print JSON to stdout
  status         Print last-run summary as JSON
  blog ingest    Upsert blog post(s) from stdin JSON
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .lib import author_vet, classify, discover, enrich, reddit, score, slack, store


def _resolve_config_dir() -> Path:
    """Resolve user-config directory with dev fallback to repo-local config/.

    Precedence:
      1. XDG/SUBSCOPE_CONFIG (see store.xdg_config_dir) IF it contains subreddits.yml
      2. Repo-local config/ next to engine/ (dev / fresh-clone workflow)
    """
    xdg = store.xdg_config_dir()
    if (xdg / "subreddits.yml").exists():
        return xdg
    repo_local = Path(__file__).resolve().parent.parent.parent / "config"
    return repo_local


CONFIG_DIR = _resolve_config_dir()

# Packaged defaults shipped in the repo `config/` dir. Once onboarding writes
# subreddits.yml into the XDG dir, _resolve_config_dir activates that dir, but it
# only holds the files onboarding generates (subreddits/keywords/brand-anchor/
# example-pains). Base defaults the user is NOT asked to author (weights.yml, and
# any future shipped default) live here, so _load_yaml falls back to this dir
# per file. Without the fallback a freshly onboarded profile crashes on the first
# scan with a missing weights.yml.
PACKAGED_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def _load_yaml(name: str, optional: bool = False) -> dict[str, Any]:
    """Load a YAML config by name, user dir first then packaged defaults.

    Precedence: the active CONFIG_DIR (the user's XDG dir once onboarded) wins,
    so user edits always override. A file absent from CONFIG_DIR falls back to
    the packaged default in PACKAGED_CONFIG_DIR, so base defaults the onboarding
    synthesizer does not emit (weights.yml) still load. An optional file missing
    from both returns {}; a required file missing from both raises with a message
    naming both search dirs.
    """
    path = CONFIG_DIR / name
    if not path.exists() and CONFIG_DIR != PACKAGED_CONFIG_DIR:
        fallback = PACKAGED_CONFIG_DIR / name
        if fallback.exists():
            path = fallback
    if not path.exists():
        if optional:
            return {}
        raise FileNotFoundError(
            f"config {name!r} not found in {CONFIG_DIR} or packaged defaults "
            f"{PACKAGED_CONFIG_DIR}"
        )
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _resolve_blog_alias(alias: str, aliases: dict[str, str]) -> str:
    return aliases.get(alias, alias)


# Valid pattern modes. Adding a new pattern? Add an entry here + drop a
# config/keywords-<mode>.yml + emoji prefix in PATTERN_EMOJI below.
VALID_MODES = {
    "default", "stack-audit", "churn", "pricing-rage",
    "build-vs-buy", "rfp-bait", "resurrect", "rivals",
}

PATTERN_EMOJI = {
    "default": "",
    "stack-audit": "🧱",
    "churn": "⚡",
    "pricing-rage": "🔥",
    "build-vs-buy": "⚖️",
    "rfp-bait": "🤝",
    "resurrect": "🪦",
    "rivals": "🥷",
}

# Per-mode fetch strategy. `default` maps to None, which keeps the historical
# fetch_delta(/new) path byte-for-byte. Every other mode fetches via the keyless
# search.rss path (reddit.fetch_search) with a per-mode sort + timeframe and an
# optional client-side age band (age_min_h..age_max_h, in hours; age_max_h None
# means no upper bound). sort=new ignores t= (Reddit only honors t= for
# sort=top/relevance), so those modes window by created_utc client-side. The
# ~408h (17d) ceiling is the live-measured depth of a restrict_sr search on
# sort=new; resurrect uses sort=top + a 6-18 month band.
SEARCH_STRATEGY: dict[str, dict[str, Any] | None] = {
    "default":      None,
    "stack-audit":  {"sort": "new", "t": None,   "limit": 25, "age_min_h": 0,    "age_max_h": 408},
    "churn":        {"sort": "new", "t": None,   "limit": 25, "age_min_h": 0,    "age_max_h": 408},
    "build-vs-buy": {"sort": "new", "t": None,   "limit": 25, "age_min_h": 0,    "age_max_h": 408},
    "rfp-bait":     {"sort": "new", "t": None,   "limit": 25, "age_min_h": 0,    "age_max_h": 408},
    "rivals":       {"sort": "new", "t": None,   "limit": 25, "age_min_h": 0,    "age_max_h": 168},
    "pricing-rage": {"sort": "new", "t": None,   "limit": 25, "age_min_h": 0,    "age_max_h": 168},
    "resurrect":    {"sort": "top", "t": "year", "limit": 25, "age_min_h": 4320, "age_max_h": 13140},
}


def _load_configs(mode: str = "default") -> dict[str, Any]:
    """Load subs + mode-specific keywords + mode-specific weight overrides.

    Mode override files (`keywords-<mode>.yml`, `weights-<mode>.yml`) are
    optional. When present, they shadow the base files entirely (not merge —
    pattern-specific gates can be radically different from default).
    """
    if mode not in VALID_MODES:
        raise ValueError(f"unknown --mode {mode!r}; valid: {sorted(VALID_MODES)}")

    subs_cfg = _load_yaml("subreddits.yml")
    base_kw = _load_yaml("keywords.yml")
    base_w = _load_yaml("weights.yml")

    # Mode-specific overrides
    if mode != "default":
        mode_kw = _load_yaml(f"keywords-{mode}.yml", optional=True)
        mode_w = _load_yaml(f"weights-{mode}.yml", optional=True)
        keywords = mode_kw or base_kw
        weights = {**base_w, **mode_w} if mode_w else base_w
    else:
        keywords = base_kw
        weights = base_w

    aliases = subs_cfg.get("blog_aliases", {})
    subs = subs_cfg.get("subreddits", [])
    for s in subs:
        s["backing_blogs"] = [_resolve_blog_alias(a, aliases) for a in s.get("backing_blogs", [])]

    # brand_anchor: the user's competitors + tools their ICP touches. Optional
    # (default NodeSparks profile has none). When present, it drives the
    # brand-match feature so the gate/judge reflect the USER's business instead
    # of the hardcoded SaaS list. Empty list = fall back to SAAS_BRANDS.
    brand_cfg = _load_yaml("brand-anchor.yml", optional=True)
    brands = [b for b in (brand_cfg.get("brand_anchor") or []) if isinstance(b, str) and b.strip()]

    return {"subs": subs, "keywords": keywords, "weights": weights,
            "brands": brands, "mode": mode}


def _bucket_keywords(bucket: str, kw: dict[str, Any]) -> list[str]:
    return list(set(kw.get("shared", []) + kw.get(bucket, [])))


def _build_mode_query(mode: str, bucket_kw: list[str], brands: list[str]) -> str:
    """Build a within-sub search query for a pattern mode.

    OR-joins a bounded, deterministic set of the mode's keyword bucket (multi-word
    terms quoted as phrases) plus up to two brand anchors for co-occurrence. The
    `rivals` mode leads with brand names (its whole job is competitor mentions);
    other modes lead with category keywords. Sorted for run-to-run reproducibility
    (the keyword bucket comes from a set()). Bounded so the URL-quoted query stays
    under reddit.QUERY_MAX_LEN (fetch_search truncates again as a backstop).
    """
    def _q(term: str) -> str:
        term = (term or "").strip()
        if not term:
            return ""
        return f'"{term}"' if " " in term else term

    if mode == "rivals" and brands:
        raw = [_q(b) for b in brands[:6]]
    else:
        raw = [_q(k) for k in sorted(bucket_kw)[:18]]
        raw += [_q(b) for b in brands[:2]] if brands else []

    seen: set[str] = set()
    terms: list[str] = []
    for term in raw:
        low = term.lower()
        if term and low not in seen:
            seen.add(low)
            terms.append(term)

    q = " OR ".join(terms)
    if len(q) > reddit.QUERY_MAX_LEN:
        q = q[:reddit.QUERY_MAX_LEN].rsplit(" ", 1)[0]
    return q


def _fetch_mode_search(
    sub: dict[str, Any], mode: str, strat: dict[str, Any],
    keywords: dict[str, Any], brands: list[str], limit_per_sub: int,
) -> list[dict[str, Any]]:
    """Fetch candidate posts for a search-based mode via reddit.fetch_search.

    Within-sub search (restrict_sr) using the mode's sort/timeframe, then a
    client-side age band so each mode keeps its intended window (resurrect 6-18mo,
    pricing-rage/rivals 7d, others ~17d). Falls back to /new for non-resurrect
    modes only when the search feed is UNREACHABLE (None); resurrect never falls
    back, since a ~3.5-day /new window cannot contain a 6-18 month thread. Skips
    removed/locked, returns newest-first, capped at limit_per_sub.
    """
    bucket_kw = _bucket_keywords(sub["bucket"], keywords)
    q = _build_mode_query(mode, bucket_kw, brands)
    cap = min(limit_per_sub, int(strat.get("limit", 25)))
    age_min_h = float(strat.get("age_min_h", 0) or 0)
    age_max_h = strat.get("age_max_h")

    def _band(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in posts:
            if p.get("removed") or p.get("locked"):
                continue
            ah = score.age_hours(p)
            if ah < age_min_h:
                continue
            if age_max_h is not None and ah > float(age_max_h):
                continue
            out.append(p)
            if len(out) >= limit_per_sub:
                break
        return out

    # A query-less search is meaningless (misconfigured profile). Non-resurrect
    # modes fall back to /new with a fresh window; resurrect contributes nothing.
    if not q:
        return [] if mode == "resurrect" else reddit.fetch_delta(
            sub["name"], None, max_limit=limit_per_sub)

    posts = reddit.fetch_search(
        sub["name"], q, sort=strat["sort"], t=strat.get("t"),
        limit=cap, restrict_sr=True,
    )
    if posts is None:
        # Unreachable. Pass None cursor so the /new fallback is not watermark-starved.
        return [] if mode == "resurrect" else reddit.fetch_delta(
            sub["name"], None, max_limit=limit_per_sub)

    if mode == "resurrect":
        banded = _band(posts)
        # Widen to all-time top for the 12-18 month tail only when the year page
        # came back FULL (cap posts) yet light on in-band hits, i.e. the page was
        # dominated by sub-6-month posts and older threads likely exist. A thin
        # year page (sub just has little history) is not worth a second GET on
        # every sub: this is the rate-limit guard, search shares the per-IP bucket.
        if len(banded) < cap and len(posts) >= cap:
            more = reddit.fetch_search(
                sub["name"], q, sort="top", t="all", limit=cap, restrict_sr=True)
            if more:
                by_id = {p["id"]: p for p in posts}
                for p in more:
                    by_id.setdefault(p["id"], p)
                banded = _band(list(by_id.values()))
        return banded

    return _band(posts)


def cmd_setup() -> None:
    store.bootstrap()
    cfg = _load_configs()
    with store.connect() as conn:
        for s in cfg["subs"]:
            store.upsert_subreddit(conn, s)
        # Seed blog_posts from blog-map.yml so first run has knowledge map.
        # blog-map.yml is OPTIONAL — many users won't have a blog.
        blog_map = _load_yaml("blog-map.yml", optional=True)
        for blog in blog_map.get("blog_posts", []):
            store.upsert_blog_post(conn, {
                "url": blog["url"],
                "title": blog["title"],
                "pain": blog["pain"],
                "saas_replaced": blog["saas_replaced"],
                "persona": blog["persona"],
                "stack": blog["stack"],
                "keywords": blog.get("keywords", []),
            })
    print(json.dumps({"status": "ok", "subs": len(cfg["subs"])}))


def cmd_orient() -> None:
    """Print the locked welcome + fork message for first-launch UX.

    Voice locked per ui-ux Phase 9.5 redesign: 3-question routing REPLACES
    the preset menu (validated by all 4 research agents — generic preset
    alone produces 3/10 ICP-match per surface, 3-question routing reaches
    5-7/10). Preset becomes the 'type preset to skip' escape hatch.

    Operational language only ("targeting", "subreddits", "config") — no
    profiling vocabulary ("ICP", "profile", "audience") that signals lead-
    gen tooling.
    """
    welcome = (
        "subscope installed. This runs locally in your Claude session and "
        "posts nothing without you.\n"
    )
    fork = (
        "\nThree quick questions so /subscope-run targets the right "
        "subreddits — about 60 seconds.\n\n"
        "  Start with: /subscope-onboard\n\n"
        "Other paths:\n"
        "  /subscope-onboard preset  — skip questions, pick a generic lane (~30s)\n"
        "  /subscope-profile         — 8-question deep interview (~12min, "
        "sharper targeting)\n"
    )
    print(welcome + fork)


def _is_backfill_eligible(reason: str, post: dict[str, Any]) -> bool:
    """Freshness/backfill pool contract: a near-miss qualifies ONLY if it named
    a specific SaaS brand AND failed a softer signal (keyword density, intent).

    The brand requirement is ENFORCED here in code, not inferred from the gate.
    The tier gates (score._tier1_gate / _tier2_gate) check keyword_density BEFORE
    the no_saas_brand check and short-circuit on first failure, so a post with no
    keywords AND no brand carries the soft reason `tier1_keyword_density`, not
    `tier1_no_saas_brand`. Without this re-check, brandless posts leak into the
    pool and get surfaced by the freshness floor / minimum backfill when nothing
    real passes the gate, which reads as off-intent noise.
    """
    backfill_disallowed = (
        score.ABSOLUTE_REJECT_REASONS
        | {"tier1_post_age", "tier2_post_age",
           "tier1_no_saas_brand", "tier2_no_saas_brand"}
    )
    if reason in backfill_disallowed:
        return False
    return score.names_specific_saas(post.get("title", ""), post.get("body", ""))


def cmd_fetch_score(
    limit_per_sub: int = 25,
    daily_cap: int | None = None,           # None = read from weights.yml pattern_caps
    no_cool: bool = False,
    cool_minutes: int | None = None,        # None = read from weights.yml
    mode: str = "default",
    no_slack: bool = False,
    max_surfaces: int | None = None,        # power-user override, see weights.yml note
    no_enrich: bool = False,                # kill switch for DFS + Firecrawl cache reads
    explain: bool = False,                  # diagnostic: emit per-post gate inputs + drop reason
    candidates: bool = False,               # judge-first: emit a candidates list for the skill judge
    search: bool = False,                   # force the search.rss fetch path even for default mode
    since_days: int | None = None,          # override active mode age window to the last N days
    window: str | None = None,              # override search recency: new|week|month|year|all
    max_requests: int | None = None,        # per-run Reddit GET ceiling (None = engine default)
) -> None:
    """Run the surface pipeline for a specific pattern mode.

    Modes (see VALID_MODES):
      default        — general pain + named SaaS
      stack-audit    — OPs listing many tools, asking to consolidate
      churn          — switching/canceling + vendor anchor
      pricing-rage   — price-hike threads (auto --no-cool)
      build-vs-buy   — debates with numbers
      rfp-bait       — "A vs B vs C" comparison threads
      resurrect      — 6-18mo old high-quality threads
      rivals         — competitor mention digest (uses brand_anchor from config)

    Each mode optionally loads `config/keywords-<mode>.yml` and
    `config/weights-<mode>.yml`. Missing mode-config falls back to default.
    """
    cfg = _load_configs(mode=mode)
    weights = cfg["weights"]
    brands = cfg.get("brands") or []
    # Judge-first candidate cap: a safety bound on how many candidates the skill
    # judge reads. Set generously: a daily run fetches well under this, and the
    # judge (Claude) handles dozens of short posts cheaply, so truncation should
    # almost never fire and never silently hide a relevant post. The rank only
    # decides WHICH survive when a run genuinely overflows.
    judge_cfg = weights.get("judge_candidates", {}) or {}
    max_candidates = int(judge_cfg.get("max", 80))

    # Plumb the --no-enrich flag through the module-level toggle so every
    # downstream cache lookup honors it (mirrors set_disabled in classify.py).
    enrich.set_disabled(no_enrich)
    # Resolve pattern-specific caps + cooling from weights.yml if not overridden
    out_cfg = weights.get("daily_output", {})
    if daily_cap is None:
        pattern_caps = out_cfg.get("pattern_caps") or {}
        daily_cap = int(pattern_caps.get(mode, out_cfg.get("default_target", 10)))
    if cool_minutes is None:
        cool_cfg = weights.get("cooling", {})
        if mode == "pricing-rage":
            cool_minutes = int(cool_cfg.get("pricing_rage_minutes", 0))
        elif mode == "resurrect":
            cool_minutes = int(cool_cfg.get("resurrect_minutes", 30))
        else:
            cool_minutes = int(cool_cfg.get("default_minutes", 15))
    # pricing-rage = time-sensitive: zero cooling
    if mode == "pricing-rage" and not no_cool and cool_minutes == 0:
        no_cool = True

    with store.connect() as conn:
        run_id = store.start_run(conn)
        # Promote mature drafts BEFORE this run so today's surfaces compete
        # cleanly. Decay old surfaces at the same time.
        promoted = store.flush_cooling_queue(conn, cool_minutes=cool_minutes)
        decayed = store.decay_old_surfaces(conn, days=14)
        blog_posts = store.fetch_blog_posts(conn)

        all_candidates: list[dict[str, Any]] = []
        near_miss_pool: list[dict[str, Any]] = []
        authority_pool: list[dict[str, Any]] = []
        fetch_errors: list[str] = []
        total_fetched = 0
        dropped_counts: dict[str, int] = {}
        explain_rows: list[dict[str, Any]] = []
        cand_list: list[dict[str, Any]] = []

        # Authority track config (dual-track surfaces). Default OFF unless the
        # weights.yml authority_track block sets enabled. When disabled, the
        # authority pool is never built and the run reverts to today's
        # buyer-only behavior exactly.
        authority_cfg = weights.get("authority_track", {}) or {}
        authority_enabled = bool(authority_cfg.get("enabled", False))

        # Reset feed counters + rate state so we can classify the run as ok /
        # rate_limited / blocked (drives the JSON `status` field). The GET
        # budget bounds the run: Reddit's keyless RSS serves ~1 request per
        # 60s window, so an unbounded run is an open-ended one.
        reddit.reset_fetch_stats()
        reddit.set_request_budget(
            reddit.DEFAULT_REQUEST_BUDGET if max_requests is None else max_requests
        )
        subs_skipped_rate_limit = 0

        sub_list = cfg["subs"]

        # Batched prefetch for the default /new path. One combined
        # /r/a+b+c/new/.rss request covers a whole batch of subs, which is the
        # difference between a run that finishes and one that spends a minute
        # per sub. fetch_delta below reads the prefetch, so the per-sub call
        # and its delta contract are unchanged. Search-driven modes hit
        # search.rss with a per-sub query, so they cannot share a feed and stay
        # on the per-sub network path.
        primed = SEARCH_STRATEGY.get(mode) is None and not search
        if primed:
            unfetched = reddit.prime_new_cache(
                sub_list, limit=max(limit_per_sub, reddit.MULTI_FEED_LIMIT))
            if unfetched:
                subs_skipped_rate_limit += len(unfetched)
                sys.stderr.write(
                    f"[subscope] {len(unfetched)} sub(s) not fetched "
                    f"(rate limit or request budget): {', '.join(unfetched)}\n"
                )
                sys.stderr.flush()

        for sub_idx, s in enumerate(sub_list):
            # Graceful partial results on the per-sub network path: if the
            # bucket drained, stop bursting and return what we already have
            # rather than hammering into 429s and failing the run wholesale.
            # The primed path has already done its network work, so breaking
            # here would only discard posts we already hold in memory.
            if not primed and reddit.is_rate_limited():
                subs_skipped_rate_limit += len(sub_list) - sub_idx
                sys.stderr.write(
                    f"[subscope] rate-limited mid-run, skipping {len(sub_list) - sub_idx} "
                    f"remaining sub(s); returning partial results\n"
                )
                sys.stderr.flush()
                break


            db_sub = store.get_sub(conn, s["name"])
            if not db_sub:
                store.upsert_subreddit(conn, s)
                db_sub = store.get_sub(conn, s["name"])
            assert db_sub is not None

            last_cursor = db_sub.get("last_cursor")
            # Resolve the fetch strategy. default -> None -> historical /new delta
            # path, unchanged. --search forces a neutral search path onto default
            # mode (onboarding recovery + power users). --since-days / --window
            # then refine whatever strategy is active.
            strat = SEARCH_STRATEGY.get(mode)
            if strat is None and search:
                strat = {"sort": "new", "t": None, "limit": limit_per_sub,
                         "age_min_h": 0, "age_max_h": 408}
            if strat is not None:
                if since_days is not None:
                    strat = {**strat, "age_max_h": max(1, int(since_days)) * 24}
                    # A recency override (e.g. --since-days 7) on an old-threads mode
                    # like resurrect (age_min_h 4320) would make min > max and band
                    # out everything. Honor the user's recency intent: drop the floor.
                    if strat.get("age_min_h", 0) > strat["age_max_h"]:
                        strat = {**strat, "age_min_h": 0}
                if window is not None:
                    strat = ({**strat, "sort": "new", "t": None} if window == "new"
                             else {**strat, "sort": "top", "t": window})
                    # A recency-window override means "give me posts from this
                    # window", so drop a mode's old-threads floor (resurrect's
                    # 6-month age_min) that would otherwise band them all out.
                    strat = {**strat, "age_min_h": 0}
            try:
                if strat is None:
                    # default mode: UNCHANGED /new delta path (byte-for-byte).
                    posts = reddit.fetch_delta(s["name"], last_cursor, max_limit=limit_per_sub)
                else:
                    posts = _fetch_mode_search(
                        s, mode, strat, cfg["keywords"], brands, limit_per_sub)
            except Exception as e:
                fetch_errors.append(f"r/{s['name']}: {e}")
                continue

            total_fetched += len(posts)
            # Only the default /new path advances the last_cursor watermark. Search
            # modes rely on the surfaced-PK dedup, not the cursor; advancing it from
            # a search result (which is not the newest /new post) would corrupt the
            # next default run's delta window.
            if strat is None and posts:
                store.update_cursor(conn, s["name"], posts[0]["id"])

            bucket_kw = _bucket_keywords(s["bucket"], cfg["keywords"])

            for post in posts:
                if store.already_surfaced(conn, post["id"]):
                    continue

                blog_matches = score.find_blog_matches(post, blog_posts)

                # Lexical gate FIRST (no network). The gate + backfill + authority
                # eligibility checks are pure keyword/intent logic, so we run them
                # before touching the network-bound author vet. A post is spared
                # the early drop only if it can still surface: a buyer survivor, a
                # backfill-eligible near-miss, OR an authority-eligible reject.
                passes, reason = score.evaluate_gate(
                    post, dict(s, **db_sub), blog_matches, weights, bucket_kw
                )
                if explain:
                    _t = post.get("title", "")
                    _b = post.get("body", "") or ""
                    _nhits, _matched = score.count_keyword_hits(post, bucket_kw)
                    explain_rows.append({
                        "id": post["id"],
                        "sub": s["name"],
                        "tier": s.get("tier"),
                        "title": _t[:80],
                        "passes": passes,
                        "reason": reason,
                        "age_h": round(score.age_hours(post), 1),
                        "kw_hits": _nhits,
                        "matched_kw": _matched,
                        "names_brand": score.names_specific_saas(_t, _b),
                        "question_intent": score.has_question_intent(_t, _b),
                        "pain_intent": score.has_pain_markers(_t, _b),
                    })
                # Judge-first candidate emission (SS-110). Recall pre-filter: any
                # fetched post that clears the ABSOLUTE rejects is a candidate for
                # the skill-layer offer-relevance judge. The engine attaches
                # deterministic features (brand match uses the user's brand_anchor)
                # and a recall rank; it does NOT decide relevance here. Capped after
                # the loop so the judge reads a bounded set.
                if candidates and (passes or reason not in score.ABSOLUTE_REJECT_REASONS):
                    _t = post.get("title", "")
                    _b = post.get("body", "") or ""
                    _n, _m = score.count_keyword_hits(post, bucket_kw)
                    _intent = score.has_question_intent(_t, _b) or score.has_pain_markers(_t, _b)
                    _brand = score.names_relevant_brand(_t, _b, brands)
                    _age = score.age_hours(post)
                    # Freshness tiebreak is meaningless for resurrect (every post
                    # is months old by design), so zero it there rather than let it
                    # uniformly bury the whole mode's recall ordering.
                    _fresh = 0.0 if mode == "resurrect" else max(0.0, (48.0 - _age) / 48.0)
                    _rank = (
                        (2.0 if _intent else 0.0)
                        + (3.0 if _brand else 0.0)
                        + float(_n)
                        + _fresh  # freshness 0..1
                    )
                    cand_list.append({
                        "id": post["id"],
                        "sub": s["name"],
                        "tier": s.get("tier"),
                        "title": _t,
                        "body": _b[:1000],
                        "url": post.get("url", ""),
                        "age_h": round(_age, 1),
                        "kw_hits": _n,
                        "matched_kw": _m,
                        "names_brand": _brand,
                        "question_intent": score.has_question_intent(_t, _b),
                        "pain_intent": score.has_pain_markers(_t, _b),
                        "engagement_available": post.get("upvote_ratio") is not None,
                        "soft_reason": "gate_pass" if passes else reason,
                        "_rank": _rank,
                    })

                backfill_eligible = (not passes) and _is_backfill_eligible(reason, post)
                # DESIGN FIX 1: authority-eligible brandless posts (no_saas_brand /
                # no_intent) are NOT backfill-eligible, so without this clause the
                # early drop below would cull them before they reach authority_pool,
                # leaving the authority track nearly empty. Spare them here.
                authority_eligible = (
                    authority_enabled and (not passes)
                    and reason in score.AUTHORITY_ELIGIBLE_REASONS
                )
                if not passes and not backfill_eligible and not authority_eligible:
                    dropped_counts[reason] = dropped_counts.get(reason, 0) + 1
                    continue

                # Author pre-gate (Phase 1.5): drop young / low-karma / wrong-
                # audience OPs before scoring. Cached 7d (consulted before any
                # network call). Gated behind the lexical gate above so we only
                # spend an author comments-RSS GET on posts that could surface,
                # keeping us well under Reddit's RSS rate-limit budget.
                #
                # DESIGN FIX 2 (lazy authority vetting): the 403 fix deliberately
                # vets only buyer gate-passers + backfill-eligible near-misses to
                # stay under the RSS rate limit. Authority-eligible posts are the
                # LARGEST bucket (every keyword-density-passing brandless post), so
                # vetting them here would re-pressure the exact limit the 403 fix
                # hardened. A post that is ONLY authority-eligible (not a buyer
                # survivor and not backfill-eligible) is therefore NOT vetted in
                # the loop: its vet is deferred to _select_authority, which only
                # vets the small set of authority-gate survivors. A post that is
                # also backfill-eligible IS vetted here, and that vet is reused.
                vet_needed = passes or backfill_eligible
                if vet_needed:
                    vet = author_vet.vet_author(post.get("author", ""), conn=conn, weights=weights)
                    if vet["verdict"] == "fail":
                        drop_reason = f"author_vet_{vet['reason']}"
                        dropped_counts[drop_reason] = dropped_counts.get(drop_reason, 0) + 1
                        # A buyer-rejected post that fails author vetting is also
                        # disqualified from the authority track (vetting is shared).
                        continue
                else:
                    # Authority-only candidate: defer the network vet. The vet
                    # still runs before authority scoring/selection, just lazily
                    # inside _select_authority on the gate-survivor subset.
                    vet = None

                # compute_score is buyer scoring. Skip it for authority-only
                # candidates (they are rescored with compute_authority_score in
                # _select_authority); leave a 0.0 placeholder so the dict shape is
                # stable for any reader.
                post["score_internal"] = (
                    score.compute_score(post, dict(s, **db_sub), blog_matches, weights, bucket_kw)
                    if vet_needed else 0.0
                )
                candidate = {
                    "post": post,
                    "sub": dict(s, **db_sub),
                    "blog_matches": blog_matches,
                    "gate_reason": reason,
                    "vet": vet,
                    "bucket_kw": bucket_kw,
                }
                if passes:
                    # Optional Phase 2 LLM classifier: only fires on regex-gate
                    # survivors (~5% of fetched volume) so cost stays bounded.
                    # If provider disabled / fails, classifier=None and the post
                    # surfaces on regex strength alone (graceful degradation).
                    verdict = classify.classify(post)
                    if verdict is not None:
                        post["classifier"] = verdict
                        # Vendor content slipped through regex? Drop here.
                        if verdict["intent"] == "vendor_content":
                            reason = "classifier_vendor"
                            dropped_counts[reason] = dropped_counts.get(reason, 0) + 1
                            continue
                        # Boost score by LLM fit_score (0-10 → 0-30 bonus).
                        post["score_internal"] = post["score_internal"] + (verdict["fit_score"] * 3)
                    all_candidates.append(candidate)
                else:
                    # Gate-failed but kept because it is backfill-eligible and/or
                    # authority-eligible (filtered above). Counted once under its
                    # gate-fail reason for the dropped footer, then routed to the
                    # pool(s) it qualifies for.
                    dropped_counts[reason] = dropped_counts.get(reason, 0) + 1
                    # Backfill pool: only posts that named a specific SaaS brand
                    # AND failed a softer signal (brand check enforced in
                    # _is_backfill_eligible; the gate short-circuits before the
                    # brand check, so reason alone cannot be trusted here).
                    if backfill_eligible:
                        near_miss_pool.append(candidate)
                    # Authority pool (dual-track): a buyer-reject is a candidate
                    # for the SECOND-pass authority gate ONLY if it failed the
                    # buyer gate for an on-topic-but-not-a-buyer reason
                    # (no_saas_brand / no_intent). A keyword-density miss carries
                    # the *_keyword_density reason, never *_no_saas_brand, so it
                    # cannot enter here, which preserves the brandless leak fix.
                    if authority_eligible:
                        authority_pool.append(candidate)

        # Phase B enrichment: pure cache-read augmentation on the gate-pass set
        # BEFORE selection so any link_context can inform the inline table.
        # No-op when no creds are present (default state) or when --no-enrich.
        enrich.augment_scores(all_candidates, conn)

        # Daily mixing rule per plan 2f, with minimum-floor backfill.
        # Power-user override: --max-surfaces N bypasses weights.yml hard_ceiling.
        selected = _select_daily(all_candidates, near_miss_pool, weights, daily_cap,
                                 max_surfaces=max_surfaces)
        if max_surfaces and max_surfaces > 12:
            _emit_max_surfaces_warning(max_surfaces)

        # Authority track (dual-track surfaces): SECOND pass over the soft-reject
        # pool, with an INDEPENDENT cap. Buyer-selected posts are excluded so a
        # post never double-surfaces. No-op (and zero authority surfaces) when
        # the flag is disabled. Authority disposition is folded into
        # dropped_counts so the footer can show why candidates were not promoted.
        buyer_ids = {s["post"]["id"] for s in selected}
        authority_selected = _select_authority(
            authority_pool, buyer_ids, weights, dropped_counts, conn=conn,
        )

        # Persist. Each surface is isolated so one bad row does not kill the run.
        persist_errors: list[str] = []
        kept_for_output: list[dict[str, Any]] = []
        surface_state = "hot" if no_cool else "drafting"
        for track, track_list in (("buyer", selected), ("authority", authority_selected)):
            for s in track_list:
                post = s["post"]
                sub_row = s["sub"]
                try:
                    store.insert_post(conn, post)
                    store.update_score(conn, post["id"], post["score_internal"])
                    for m in s["blog_matches"]:
                        store.record_blog_ref(
                            conn, post["id"], m["url"], m["match_score"], m["matched_keywords"]
                        )
                    # Cooling queue: surfaces land 'drafting' (held cool_minutes)
                    # unless --no-cool is set. Notion sync flushes only 'hot' rows.
                    store.mark_surfaced(conn, post["id"], run_id, sub_row["tier"],
                                        state=surface_state)
                except Exception as e:
                    persist_errors.append(f"{post.get('id', '?')}: {e}")
                    continue
                s["pain_summary"] = _pain_summary(post, s["blog_matches"])
                s["fit_summary"] = _fit_summary(sub_row, s["blog_matches"])
                s["score_internal"] = post["score_internal"]
                s["pattern"] = mode
                s["pattern_emoji"] = PATTERN_EMOJI.get(mode, "")
                s["track"] = track
                kept_for_output.append(s)
        # Split back out for the renderers (both keep only successfully persisted rows).
        selected = [s for s in kept_for_output if s["track"] == "buyer"]
        authority_selected = [s for s in kept_for_output if s["track"] == "authority"]

        total_surfaced = len(selected) + len(authority_selected)
        all_notes = fetch_errors + [f"persist:{e}" for e in persist_errors]
        notes = "; ".join(all_notes) if all_notes else ""
        store.finish_run(conn, run_id, total_fetched, total_surfaced, notes)

    # Run verdict, three states:
    #   rate_limited : a 429 landed or we stopped early to avoid one. The run may
    #                  be partial; the user should re-run in a minute. This takes
    #                  precedence over "blocked" (it is transient, not an edge ban).
    #   blocked      : every feed GET failed for a NON-429 reason and nothing was
    #                  fetched (true edge block / network down).
    #   ok           : at least one feed fetched, no rate-limit hit. A zero-surface
    #                  result here is a genuinely quiet day.
    fetch_stats = reddit.get_fetch_stats()
    rate_limited = fetch_stats.get("rate_limited", 0) > 0 or subs_skipped_rate_limit > 0
    if rate_limited:
        status = "rate_limited"
        # Count = feeds that 429'd plus subs we deliberately skipped to avoid one.
        dropped_counts["fetch_rate_limited"] = (
            fetch_stats.get("rate_limited", 0) + subs_skipped_rate_limit
        )
    elif fetch_stats["ok"] == 0 and fetch_stats["failed"] > 0:
        status = "blocked"
        # Surface it in dropped_counts too, so consumers reading only that dict
        # still see the block. Count = number of failed feed GETs.
        dropped_counts["fetch_blocked"] = fetch_stats["failed"]
    else:
        status = "ok"

    from .lib import output as out_mod
    payload = {
        "run_id": run_id,
        "mode": mode,
        "status": status,
        "fetched": total_fetched,
        "surfaced": total_surfaced,
        "buyer_count": len(selected),
        "authority_count": len(authority_selected),
        "subs_skipped_rate_limit": subs_skipped_rate_limit,
        "subs_scanned": len(sub_list) - subs_skipped_rate_limit,
        "reddit_requests": reddit.requests_used(),
        "fetch_stats": fetch_stats,
        "dropped_counts": dropped_counts,
        "notes": notes,
        "surfaces": (out_mod.render_json_payload(selected, track="buyer")
                     + out_mod.render_json_payload(authority_selected, track="authority")),
        "inline_markdown": out_mod.render(
            selected, notes, dropped_counts, authority_surfaces=authority_selected),
        "inline_table": out_mod.render_table(
            selected, dropped_counts, authority_surfaces=authority_selected),
    }
    if explain:
        payload["explain"] = explain_rows
    if candidates:
        # Rank by the recall heuristic, cap, then drop the internal sort key.
        cand_list.sort(key=lambda c: c["_rank"], reverse=True)
        capped = cand_list[:max_candidates]
        for c in capped:
            c.pop("_rank", None)
        payload["candidates"] = capped
        payload["candidate_count"] = len(capped)
        payload["candidate_total"] = len(cand_list)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    # Optional Slack push: silent no-op when no webhook configured. Never
    # blocks the run; failure is logged to stderr only.
    if not no_slack:
        slack.notify_if_configured(payload)


def _select_daily(candidates: list[dict[str, Any]], near_miss_pool: list[dict[str, Any]],
                  weights: dict[str, Any], daily_cap: int,
                  max_surfaces: int | None = None) -> list[dict[str, Any]]:
    """Pick daily surfaces. Hard ceiling, per-sub caps, freshness floor, and
    minimum-floor backfill.

    Order of priority:
      1. Gate-passing Tier 1 posts (top N by score, capped per sub)
      2. Gate-passing Tier 2 posts (top by score, capped per sub)
      3. Freshness floor: auto-promote any near-miss post < `freshness_floor.max_age_hours`
         old, up to `freshness_floor.max_promoted`. Bypasses per-sub caps because
         freshness IS the signal. Tagged `freshness_promoted=True` for telemetry.
      4. If still under `minimum`, backfill from remaining near-miss pool by score.
         During backfill ONLY, per-sub caps relax by `backfill_sub_cap_bonus`
         so a sub that already supplied a hot Tier-2 surface can still backfill.

    `max_surfaces` is the power-user override (--max-surfaces flag). Beats
    weights.yml hard_ceiling when set. Per-sub caps still apply on Tier 1/Tier 2
    selection — the user can pull from more SUBS, not more posts per sub
    (quality guardrail). Freshness + backfill phases relax this.
    """
    out_cfg = weights.get("daily_output", {})
    cap = max_surfaces or int(out_cfg.get("hard_ceiling", daily_cap))
    minimum = int(out_cfg.get("minimum", 0))
    t1_per_sub = int(out_cfg.get("tier1_per_sub_cap", 2))
    t2_per_sub = int(out_cfg.get("tier2_per_sub_cap", 1))
    backfill_bonus = int(out_cfg.get("backfill_sub_cap_bonus", 0))

    fresh_cfg = weights.get("freshness_floor") or {}
    fresh_enabled = bool(fresh_cfg.get("enabled", False))
    fresh_max_age_s = int(fresh_cfg.get("max_age_hours", 24)) * 3600
    fresh_max_promoted = int(fresh_cfg.get("max_promoted", 3))

    now_ts = int(time.time())

    # Deduplicate by (subreddit, title) before selection. Two posts in
    # /new with identical titles from the same sub almost always means
    # a cross-post or duplicate by the same OP. Keep the higher-scoring one.
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for c in candidates:
        key = (c["sub"]["name"].lower(), c["post"]["title"].strip().lower())
        prev = by_key.get(key)
        if prev is None or c["post"]["score_internal"] > prev["post"]["score_internal"]:
            by_key[key] = c
    candidates = list(by_key.values())

    t1_pool = [c for c in candidates if c["sub"]["tier"] == 1]
    t2_pool = [c for c in candidates if c["sub"]["tier"] == 2]
    t1_pool.sort(key=lambda c: c["post"]["score_internal"], reverse=True)
    t2_pool.sort(key=lambda c: c["post"]["score_internal"], reverse=True)

    chosen: list[dict[str, Any]] = []
    sub_counts: dict[str, int] = {}
    chosen_ids: set[str] = set()

    def _take(c: dict[str, Any]) -> None:
        chosen.append(c)
        chosen_ids.add(c["post"]["id"])
        sub_counts[c["sub"]["name"]] = sub_counts.get(c["sub"]["name"], 0) + 1

    for c in t1_pool:
        if len(chosen) >= cap:
            break
        name = c["sub"]["name"]
        if sub_counts.get(name, 0) >= t1_per_sub:
            continue
        _take(c)

    for c in t2_pool:
        if len(chosen) >= cap:
            break
        name = c["sub"]["name"]
        if sub_counts.get(name, 0) >= t2_per_sub:
            continue
        _take(c)

    # Phase 3: Freshness floor. Auto-promote near-miss posts younger than the
    # configured window. Bypasses per-sub caps because freshness IS the signal
    # (a 2h-old post that fails keyword density is often a real ICP signal
    # the keyword set just didn't anticipate yet).
    if fresh_enabled and near_miss_pool and len(chosen) < cap:
        fresh_candidates = [
            c for c in near_miss_pool
            if c["post"]["id"] not in chosen_ids
            and (now_ts - int(c["post"].get("created_utc", 0))) < fresh_max_age_s
        ]
        fresh_candidates.sort(
            key=lambda c: int(c["post"].get("created_utc", 0)),
            reverse=True,
        )
        promoted = 0
        for c in fresh_candidates:
            if promoted >= fresh_max_promoted or len(chosen) >= cap:
                break
            c["freshness_promoted"] = True
            _take(c)
            promoted += 1

    # Phase 4: Minimum-floor backfill. If still under `minimum`, pull from the
    # near-miss pool by score. Per-sub caps relax by `backfill_sub_cap_bonus`
    # so a sub that supplied a hot Tier 2 surface (consuming its quota) can
    # still backfill. Without this, scans on narrow profiles return <minimum
    # surfaces even when good candidates exist.
    if minimum > 0 and len(chosen) < minimum and near_miss_pool:
        remaining = [
            c for c in near_miss_pool if c["post"]["id"] not in chosen_ids
        ]
        remaining.sort(key=lambda c: c["post"]["score_internal"], reverse=True)
        for c in remaining:
            if len(chosen) >= min(cap, minimum):
                break
            name = c["sub"]["name"]
            base_cap = t1_per_sub if c["sub"]["tier"] == 1 else t2_per_sub
            if sub_counts.get(name, 0) >= base_cap + backfill_bonus:
                continue
            c["backfilled"] = True
            _take(c)

    return chosen


def _select_authority(
    authority_pool: list[dict[str, Any]],
    buyer_ids: set[str],
    weights: dict[str, Any],
    dropped_counts: dict[str, int],
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    """Second-pass authority selection (dual-track surfaces).

    Runs the deterministic authority gate FIRST, then vets the author LAZILY on
    gate survivors only, then scores with compute_authority_score over the
    soft-reject pool. Drops anything the buyer track already surfaced (no
    double-surfacing), dedups by (sub, title), sorts by authority score, and
    applies the INDEPENDENT authority_track.cap. Gate + vet rejections are folded
    into dropped_counts so the footer can show authority disposition.

    Lazy author vetting (rate-limit safety): authority-eligible posts are the
    largest bucket, so the main loop does NOT vet them (it defers with
    candidate["vet"] = None). We vet here, but ONLY for posts that already PASSED
    the deterministic authority gate (question-intent + keyword-density + career/
    identity filter). That bounds author comments-RSS GETs to the small set of
    gate survivors, keeping us under Reddit's RSS rate-limit budget. The vet is
    cache-first (7-day vetted_authors table consulted before any network call). A
    candidate that already carries a vet dict (e.g. a branded no_intent post that
    was backfill-eligible and vetted in the main loop, or a synthetic test vet) is
    reused, never re-vetted.

    Returns [] when the authority track is disabled or empty. The selected
    surfaces carry post["score_internal"] = the authority score (so the renderer
    and persistence use the authority ranking, not the buyer ranking).
    """
    at_cfg = weights.get("authority_track", {}) or {}
    if not at_cfg.get("enabled", False) or not authority_pool:
        return []

    cap = int(at_cfg.get("cap", 4))
    if cap <= 0:
        return []

    qualified: list[dict[str, Any]] = []
    for c in authority_pool:
        post = c["post"]
        # No double-surfacing: a buyer-selected post never appears in authority.
        if post["id"] in buyer_ids:
            continue
        # Deterministic gate FIRST (no network): question intent + keyword
        # density + career/identity filter + absolute rejects. With vet=None its
        # final author-vet check is a defensive pass, so the network vet only
        # fires below on posts that already cleared the deterministic gate.
        passes, areason = score.evaluate_authority_gate(
            post, c["sub"], c.get("gate_reason", ""), weights,
            c.get("bucket_kw", []), vet=c.get("vet"),
        )
        if not passes:
            dropped_counts[areason] = dropped_counts.get(areason, 0) + 1
            continue
        # Lazy author vet on the gate survivor. Reuse a vet already on the
        # candidate (computed in the main loop, or supplied by a test); otherwise
        # vet now, cache-first. Drop on a failing verdict.
        vet = c.get("vet")
        if vet is None:
            vet = author_vet.vet_author(
                post.get("author", ""), conn=conn, weights=weights
            )
            c["vet"] = vet
        if vet.get("verdict") == "fail":
            drop_reason = f"author_vet_{vet.get('reason')}"
            dropped_counts[drop_reason] = dropped_counts.get(drop_reason, 0) + 1
            continue
        post["score_internal"] = score.compute_authority_score(
            post, c["sub"], c.get("blog_matches", []), weights, c.get("bucket_kw", []),
        )
        qualified.append(c)

    # Dedup by (sub, title): a cross-post or duplicate keeps the higher score.
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for c in qualified:
        key = (c["sub"]["name"].lower(), c["post"]["title"].strip().lower())
        prev = by_key.get(key)
        if prev is None or c["post"]["score_internal"] > prev["post"]["score_internal"]:
            by_key[key] = c
    deduped = list(by_key.values())
    deduped.sort(key=lambda c: c["post"]["score_internal"], reverse=True)
    return deduped[:cap]


def _pain_summary(post: dict[str, Any], blog_matches: list[dict[str, Any]]) -> str:
    if blog_matches:
        kws = blog_matches[0].get("matched_keywords", [])
        if kws:
            return f"Matches blog keywords: {', '.join(kws[:4])}"
    title = post.get("title", "")
    return title[:120]


def _fit_summary(sub_row: dict[str, Any], blog_matches: list[dict[str, Any]]) -> str:
    sat = sub_row.get("saturation")
    base = f"r/{sub_row['name']}"
    if sub_row["tier"] == 1:
        base += " is a Tier 1 daily-scan sub"
    elif sat == "wide_open":
        base += " (wide-open: low competitor density)"
    elif sat == "high":
        base += " (high saturation: gate verified)"
    if blog_matches:
        return f"{base}, backed by {blog_matches[0]['title']}"
    return f"{base}. No direct blog backing, topic-adjacent reply."


def cmd_status() -> None:
    with store.connect() as conn:
        last = store.stats_last_run(conn)
        enrichment_state = enrich.status(conn)
    print(json.dumps({
        "last_run": last,
        "config_dir": str(CONFIG_DIR),
        "db_path": str(store.db_path()),
        "llm": classify.status(),
        "slack": slack.status(),
        "enrichment": enrichment_state,
    }, default=str, indent=2))


def cmd_blog_ingest() -> None:
    """Read blog post JSON from stdin (orchestrator-provided), upsert into blog_posts."""
    payload = json.load(sys.stdin)
    posts = payload if isinstance(payload, list) else [payload]
    with store.connect() as conn:
        for blog in posts:
            store.upsert_blog_post(conn, blog)
    print(json.dumps({"status": "ok", "ingested": len(posts)}))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="subscope")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="Bootstrap DB + load configs")
    sub.add_parser("orient", help="Print first-launch welcome + fork message")
    fs = sub.add_parser("fetch-score", help="Fetch deltas, gate, score, surface")
    fs.add_argument("--limit-per-sub", type=int, default=25)
    fs.add_argument("--max-requests", type=int, default=None,
                    help="Per-run Reddit GET ceiling. Reddit's keyless RSS serves "
                         "~1 request per 60s window, so this is also the run's "
                         "worst-case minutes (default: engine default)")
    fs.add_argument("--daily-cap", type=int, default=None,
                    help="Override pattern-specific cap (default: read from weights.yml pattern_caps)")
    fs.add_argument("--no-cool", action="store_true",
                    help="Skip cooling queue — surfaces immediately visible "
                         "(use for time-sensitive patterns like pricing-rage)")
    fs.add_argument("--cool-minutes", type=int, default=None,
                    help="Cooling queue hold duration (default: read from weights.yml cooling)")
    fs.add_argument("--mode", choices=sorted(VALID_MODES), default="default",
                    help="Pattern mode — gate and keywords change per pattern")
    fs.add_argument("--no-enrich", action="store_true",
                    help="Disable DataForSEO + Firecrawl cache reads for this run.")
    fs.add_argument("--no-slack", action="store_true",
                    help="Skip Slack notification even if webhook is configured")
    fs.add_argument("--explain", action="store_true",
                    help="DIAGNOSTIC: include a per-post 'explain' list (gate inputs + "
                         "drop reason) in the JSON output. Use to see why posts were dropped.")
    fs.add_argument("--candidates", action="store_true",
                    help="JUDGE-FIRST: include a 'candidates' list (posts clearing absolute "
                         "rejects, with relevance features) for the skill-layer offer-relevance judge.")
    fs.add_argument("--max-surfaces", type=int, default=None,
                    help="POWER USER: surface up to N items (default: weights.yml hard_ceiling=12). "
                         "Reddit API is fine with the volume; review fatigue is the actual risk. "
                         "Plan ~1 min per surface. To raise the per-profile sub ceiling above 13 "
                         "total, edit tier1_subs_max / tier2_subs_max in weights.yml.")
    fs.add_argument("--search", action="store_true",
                    help="Force the keyless search.rss fetch path even for default mode (used by "
                         "the onboarding 7-day recovery scan and power users). Pattern modes already search.")
    fs.add_argument("--since-days", type=int, default=None,
                    help="Override the active mode's client-side age window to the last N days.")
    fs.add_argument("--window", choices=["new", "week", "month", "year", "all"], default=None,
                    help="Override search recency. 'new' = newest first; week/month/year/all = "
                         "sort=top with that Reddit timeframe.")

    ov = sub.add_parser("op-vet", help="Score a Reddit OP profile (utility, one-shot)")
    ov.add_argument("username", type=str)

    dc = sub.add_parser("discover", help="Live subreddit discovery from interview answers")
    dc.add_argument("--answers-json", type=str, required=True,
                    help="JSON object with keys what_offering, who_to_reach, pain_quote. "
                         "Pass '-' or '/dev/stdin' to read from stdin (avoids shell quoting issues).")
    dc.add_argument("--homepage", type=str, default="",
                    help="User homepage URL (for cache lookups: Firecrawl scrape + DFS competitors)")
    dc.add_argument("--vertical", type=str, default=None,
                    help="Optional vertical clarifier value (set on Tier-A retry)")
    dc.add_argument("--competitors", type=str, default="",
                    help="Comma-separated competitor brands or domains. Used by the "
                         "engine to generate 'replacing X' and 'X alternative' queries. "
                         "Pass these from the skill when Claude found them via WebFetch.")
    dc.add_argument("--fresh-window-hours", type=int, default=None,
                    help="Buyer-activity freshness window in hours. Default 168 (7 days) "
                         "for onboarding discovery. Pass 720 on the 'broaden' clarifier path.")
    dc.add_argument("--skip-validation", action="store_true",
                    help="Skip Phase B per-sub freshness validation (fewer Reddit calls). "
                         "Returns Phase A candidates + a relative relevance score. Used by "
                         "onboarding, which confirms sub names and lets the final scan validate.")

    sub.add_parser("status", help="Print last-run status as JSON")

    blog = sub.add_parser("blog", help="Blog map operations").add_subparsers(dest="blogcmd", required=True)
    blog.add_parser("ingest", help="Upsert blog post(s) from stdin JSON")

    args = parser.parse_args(argv)

    if args.cmd == "setup":
        cmd_setup()
    elif args.cmd == "orient":
        cmd_orient()
    elif args.cmd == "fetch-score":
        cmd_fetch_score(
            args.limit_per_sub, args.daily_cap,
            no_cool=args.no_cool, cool_minutes=args.cool_minutes,
            mode=args.mode, no_slack=args.no_slack,
            max_surfaces=args.max_surfaces,
            no_enrich=args.no_enrich,
            explain=args.explain,
            candidates=args.candidates,
            search=args.search,
            since_days=args.since_days,
            window=args.window,
            max_requests=args.max_requests,
        )
    elif args.cmd == "op-vet":
        cmd_op_vet(args.username)
    elif args.cmd == "discover":
        cmd_discover(args.answers_json, args.homepage, args.vertical,
                     args.competitors, args.fresh_window_hours,
                     skip_validation=args.skip_validation)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "blog" and args.blogcmd == "ingest":
        cmd_blog_ingest()


_MAX_SURFACES_BANNER_SHOWN = False


def _emit_max_surfaces_warning(n: int) -> None:
    """One-time stderr banner when --max-surfaces > 12. Cheap process-local
    flag (not persisted) — re-running re-prints, which is fine: the user
    explicitly opted in via the flag every time."""
    global _MAX_SURFACES_BANNER_SHOWN
    if _MAX_SURFACES_BANNER_SHOWN:
        return
    _MAX_SURFACES_BANNER_SHOWN = True
    sys.stderr.write(
        f"[cli] --max-surfaces={n}. Reddit's API is fine with this volume "
        f"(roughly 30 read req/day, well under 100 QPM). The risk is YOUR "
        f"review fatigue. Default cap of 12 exists because attention drops "
        f"80% past position 10 (Nielsen Norman). Plan ~1 min per surface.\n"
    )
    sys.stderr.flush()


def cmd_discover(answers_json: str, homepage: str, vertical: str | None,
                 competitors_csv: str = "", fresh_window_hours: int | None = None,
                 skip_validation: bool = False) -> None:
    """Live subreddit discovery for the /subscope-onboard T5 card.

    Reads JSON answers from --answers-json. Two forms accepted:
      (a) literal JSON string: --answers-json '{"what_offering": ...}'
      (b) the literal token '-' or '/dev/stdin' to read from stdin instead.
          This avoids shell-quote breakage when user input contains apostrophes
          (e.g. T4 = "we're tired of saas pricing"). The skill orchestrator
          should prefer stdin for any user-derived input.

    Writes ranked sub list + clarifier signals as JSON to stdout.
    """
    if answers_json in ("-", "/dev/stdin"):
        answers_json = sys.stdin.read()
    try:
        answers = json.loads(answers_json)
        if not isinstance(answers, dict):
            raise ValueError("answers must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        print(json.dumps({"error": f"invalid --answers-json: {e}"}))
        sys.exit(2)

    # Comma-separated competitors from skill (independent of DFS cache).
    # Strip + dedup; empty strings filtered out.
    extra_competitors = [c.strip() for c in (competitors_csv or "").split(",")
                         if c.strip()]

    kwargs = {"vertical": vertical, "extra_competitors": extra_competitors or None,
              "skip_validation": skip_validation}
    if fresh_window_hours is not None:
        kwargs["fresh_window_hours"] = fresh_window_hours

    with store.connect() as conn:
        result = discover.discover_subs_for_profile(
            answers, homepage or "", conn, **kwargs,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_op_vet(username: str) -> None:
    """Standalone OP profile scorer. Outputs JSON: {karma, age_days,
    sub_breakdown_top, verdict, reason}. Used by `/subscope-op-vet`.

    Honors weights.yml `author_vet:` block so /tune-loosened thresholds apply
    to ad-hoc op-vets too (architect's consistency note from PIPE-1 review).
    """
    weights = _load_configs(mode="default")["weights"]
    with store.connect() as conn:
        result = author_vet.vet_author(username, conn=conn, weights=weights)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
