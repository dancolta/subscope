# subscope — Contributor Guide

This file is for **contributors working on the codebase**, not plugin users. Plugin users want [README.md](README.md). Plugin behavior is defined in [`skills/`](skills/), not here. (`claude plugin validate` flags this file as "not loaded as plugin context" — that's correct, by design.)

If you opened a clone and asked Claude to help you contribute, this is the orientation document.

---

## What this codebase is

A Python engine + Claude Code skills:

- **`engine/subscope/`** — Python. Fetches Reddit via public JSON, runs regex + optional LLM gates, scores survivors, writes to SQLite, prints JSON to stdout. Stdlib + pyyaml + optional `openai`, `notion-client`.
- **`skills/*/SKILL.md`** — 15 user-invocable Claude Code skills. Each one is a single Markdown file that tells Claude how to orchestrate a workflow (Notion sync via MCP, Obsidian write via MCP, Playwright blog refresh, etc.). The Python engine does no MCP work — the skill layer does.
- **`config/`** — YAML defaults: weights, default subreddits, default keywords, scoring caps. Public users override by writing to `~/.config/subscope/`.
- **`presets/`** — 4 starter bundles (b2b-saas-founder, agency-owner, indie-hacker, consultant) for users who don't want to run `/subscope-onboard`.
- **`assets/`** — README hero GIF + the Python+Pillow render script.

The engine is intentionally separable: you could pipe its JSON output to any orchestrator, not just Claude Code.

---

## File layout

```
.
├── .claude-plugin/plugin.json    # plugin manifest (required by Claude Code)
├── engine/
│   ├── subscope/
│   │   ├── cli.py                # all CLI subcommands (fetch-score, status, op-vet, ...)
│   │   ├── lib/                  # the engine modules
│   │   │   ├── store.py          # SQLite + XDG paths + enrichment cache helpers
│   │   │   ├── score.py          # gate + score + selection
│   │   │   ├── reddit.py         # public-JSON fetcher
│   │   │   ├── classify.py       # OpenAI-compat bulk LLM grader
│   │   │   ├── author_vet.py     # OP karma/age/audience pre-gate
│   │   │   ├── discover.py       # live subreddit discovery for /onboard T5 (recall stage)
│   │   │   ├── archetype_map.py  # 6 archetypes, fallback seed for /onboard + /profile
│   │   │   ├── profile_synth.py  # 8-Q + 3-Q config synthesis
│   │   │   ├── obsidian_sync.py  # weekly pulse digest builder
│   │   │   ├── enrich.py         # DataForSEO + Firecrawl conditional consumers
│   │   │   ├── net.py            # SSRF guard + certifi-aware SSL context
│   │   │   ├── slack.py          # optional webhook push
│   │   │   ├── tune_engine.py    # /tune ranker back-prop
│   │   │   └── output.py         # markdown + table renderers
│   │   └── prompts/              # system prompts (classify, profile_synth)
│   ├── scripts/                  # one-shot helpers (write_dataforseo_config, write_firecrawl_config, notion_admin, ...)
│   └── tests/                    # pytest
├── skills/                       # 15 SKILL.md files, one per pattern
├── config/                       # default YAML (subreddits, keywords, weights, presets)
├── presets/                      # 4 starter bundles
├── assets/                       # hero.gif + render_hero.py
└── docs/                         # setup-notion.md (public only)
```

---

## Conventions

These are non-negotiable for any PR:

- **No em dashes** in any user-facing text (chat output, Notion writes, error messages). Use commas or restructure. The engine output is em-dash-free; preserve that. `engine/tests/test_no_em_dashes.py` enforces it.
- **Parameterized SQL only.** Every `conn.execute()` must use `?` placeholders. f-string SQL is a defect.
- **`chmod 600` on every config + DB file.** Atomic creation via `os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)` — never `open()` then `chmod()` (umask race).
- **XDG-compliant paths.** Config at `~/.config/subscope/` (or `$XDG_CONFIG_HOME/subscope/`), data at `~/.local/share/subscope/`. Override via `SUBSCOPE_CONFIG` / `SUBSCOPE_DATA` env for tests.
- **Reddit username validation.** Any value interpolated into a `reddit.com/user/<x>/` URL must pass `reddit._safe_username()` regex first (defuses path-segment injection).
- **No new shell=True subprocess calls.** Use `subprocess.run([..., args], shell=False)` with list args. The engine has zero `shell=True` calls today; keep it that way.
- **SSRF guard.** Any user-configurable URL (LLM endpoint, Slack webhook, future adapters) must validate scheme + host before the HTTP call. See `classify._validate_base_url()` for the pattern.
- **No telemetry, ever.** No analytics, no error reporting, no usage pings. If you need to send anything off the user's machine, it must be opt-in with a one-time stderr banner.

---

## Adding a new pattern skill

The 8 patterns share one engine (`fetch-score --mode <pattern>`). Adding a pattern is ~30 minutes:

1. Add pattern keywords: `config/keywords-<pattern>.yml`
2. Add cap in `config/weights.yml` under `pattern_caps`
3. Add the mode to `VALID_MODES` in `engine/subscope/cli.py`
4. Add an emoji in `PATTERN_EMOJI` in `cli.py`
5. Create `skills/<pattern>/SKILL.md`:

```markdown
---
name: <pattern>
description: One-paragraph description. Triggers on "<pattern>", "/subscope-<pattern>", "...".
allowed-tools: Bash, Read, Write
---

# /subscope-<pattern>

[1-line intent]

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m subscope.cli fetch-score --mode <pattern>
```

[Any pattern-specific instructions for Claude here]
```

6. Add a test stub to `engine/tests/test_<pattern>.py` (mock the fetch, assert dropped_counts shape).

That's it. The engine handles the rest.

---

## Running tests

```bash
pip install -e '.[dev]'
python3 -m pytest engine/tests/
```

274 tests, target <2s total runtime. New PRs must keep the suite green.

End-to-end smoke (live Reddit fetch, no posting):

```bash
PYTHONPATH=engine python3 -m subscope.cli fetch-score --limit-per-sub 3 --daily-cap 3 --no-slack
```

Validates the plugin manifest:

```bash
claude plugin validate .
```

---

## What NOT to add

- Auto-posting, auto-commenting, account rotation — these are deliberate omissions. The plugin's positioning is human-in-the-loop. PRs that add write-side Reddit operations will be closed.
- New SaaS dependencies — every integration must work with a free tier or stdlib-only. No Resend, Postmark, Clearbit, Apollo paid layers.
- Cross-platform adapters in v0.1.x — they're roadmapped for v0.2/v0.3. Open an issue first.
- Em dashes anywhere.

---

## Architecture decisions worth knowing

These are choices that look weird in code review but exist for a reason:

- **Two LLM-call paths** are NOT in the code despite the older Anthropic SDK being installable — we standardized on the OpenAI-compatible client because Anthropic exposes `/openai/v1`. Reverting to a separate Anthropic SDK path is dead code today; if you need prompt caching, that's the v0.2 work.
- **The cooling queue** holds new surfaces in `state=drafting` for `cool_minutes` (default 15) before promoting to `hot`. This prevents bot-detection patterns (replying within 2 minutes of post creation looks like automation). Pricing-rage mode sets this to 0 because the pattern is genuinely time-sensitive.
- **The cap is a UX filter, not a safety limit.** `hard_ceiling=12` exists because attention drops 80% past position 10 (Nielsen Norman). Reddit's API allows ~100 QPM; we use ~30 req/day. Power users override via `--max-surfaces`.
- **author_vet** runs BEFORE scoring, not after. Catches throwaway/karma-farmer OPs early so they never enter the scoring pool. Cached 7 days in SQLite (`vetted_authors` table). **Refinement (2026-05-29, RSS rate-limit fix):** the vet now runs only on posts that already passed the lexical gate (or are backfill-eligible near-misses), not on every fetched post. The vet adds a `/user/<x>/comments/.rss` GET per uncached author, and Reddit's RSS surface has a per-IP rate limit (see below), so vetting every post would drain the bucket. The vet still runs before `compute_score`/selection (the intent is preserved, bad OPs never surface), it just no longer fires a network call for posts the lexical gate already rejected. The 7-day `vetted_authors` cache is always consulted before any network call, so repeat OPs cost zero requests. `about.json` is dead (403), so karma/age gates fail open and only the audience-fit gate (rebuilt from comments RSS) actively fires. **Dual-track refinement (authority lazy vetting):** the authority track (see dual-track below) draws from the brandless soft-reject pool, which is the largest bucket, so vetting those in the fetch loop would re-pressure the RSS rate limit. Authority-only candidates are therefore NOT vetted in the loop (`candidate["vet"] = None`); the vet is deferred to `_select_authority` and fires only on posts that already passed the deterministic authority gate (cache-first), bounding author GETs to the small gate-survivor set.
- **Reddit RSS rate-limit discipline** (`reddit.py`). The keyless RSS surface enforces a per-IP token bucket (~100 req / 10 min, exposed via `x-ratelimit-*` headers). A daily run fires ~18 sub feeds plus a per-plausible-OP author feed, which trips the limit if bursted. `reddit.py` paces every GET at least `MIN_REQUEST_INTERVAL` apart, reads `x-ratelimit-remaining`/`-reset` to pause before draining, backs off on 429 (Retry-After, then `x-ratelimit-reset`, capped at `MAX_RATELIMIT_PAUSE`), and exposes `is_rate_limited()` so `cmd_fetch_score` stops mid-run and returns partial results. The run JSON reports three states: `ok`, `rate_limited` (transient, retry shortly), `blocked` (non-429 edge failure). No OAuth, no API key. Manual runs are once or twice a day, so ~30 to 60s of total spacing is acceptable.
- **The 3-question `/onboard` + 8-question `/profile` BOTH route through `profile_synth.py`.** Both seed an archetype and let Claude refine the config in chat (the in-session reasoning is the synthesis), then run the same validator and YAML writer. The old headless bulk-LLM `llm_synthesize` path was removed as dead code (zero callers), along with its `prompts/profile_synth.md` and `prompts/profile_research.md` prompt files.
- **Subreddit discovery is split: engine = recall, skill = precision (`discover.py` + `skills/onboard/SKILL.md`).** Onboarding T5 no longer seeds subs from the archetype map. The engine runs live discovery (DataForSEO SERP + Reddit search to find candidate subs, then a per-sub search-within-sub over a 7-day window to confirm each has a real buyer thread), and emits candidates with an absolute-timestamped evidence thread, a truthful `recent_thread_reason`, and a 0-100 confidence. The per-thread gate (`software_buyer_intent`) is deterministic and lexical, so it tops out ~50% precision: it cannot tell "Software Engineering vs Dentistry" (career) from "dental software vs Dentrix" (buyer). The precision layer is the SKILL.md relevance review, where the orchestrating Claude drops semantic false positives (career questions, self-promo, brand-name collisions like Clio-the-car). This split is intentional: the engine subprocess cannot rely on an LLM key (Claude Code injects `ANTHROPIC_API_KEY` into the session, not child processes), so the semantic judgment lives in the skill layer where an LLM is guaranteed present. `archetype_map.py` remains only as the thin/fallback seed when discovery is unreachable. Freshness window is 7 days for onboarding discovery vs 48h for the daily scan (`DISCOVERY_FRESH_WINDOW_HOURS` vs `PHASE_B_FRESH_WINDOW_HOURS`).

---

## License + Contributing

MIT. PRs welcome. Open an issue first for non-trivial changes — the plugin's anti-positioning surface (what it deliberately doesn't do) matters as much as the features.
