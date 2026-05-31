---
name: subscope-rivals
description: Track today's Reddit mentions of competitors from your brand_anchor config. Pure competitive intel — surfaces every substantive mention of a named vendor in the last 24h across configured subs. Triggers on "rivals scan", "/subscope-rivals", "track competitor mentions", "competitor radar".
allowed-tools: Bash, Read, Write
---

# /subscope-rivals (🥷)

Scans recent Reddit mentions of any competitor in your `brand_anchor` config (set during /subscope-onboard or /subscope-profile). Surfaces substantive mentions from the last 7 days: reviews, switching threads, pricing complaints, alternative-seeking posts.

```bash
# user invocation
/subscope-rivals
```

Engine call:

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m subscope.cli fetch-score --mode rivals --candidates
```

The rivals mode builds its Reddit search query directly from your `~/.config/subscope/brand-anchor.yml` competitor list (when that is empty it falls back to the alternative-seeking and switching keywords in `config/keywords-rivals.yml` plus the built-in brand list). It searches each configured sub for those brand names across the last 7 days via keyless Reddit search, then hands you the candidates. For a one-off scan of a brand NOT in your config, add it to brand-anchor.yml. Cooling queue applies.

Judge every entry in `candidates[]` against the user's offer with the offer-relevance judge in `skills/run/SKILL.md` Step 3.5. A substantive competitor mention (review, switching thread, pricing complaint, alternative-seeking post) is the target; a bare brand name with no buyer or discussion value is REJECT. Surface only BUYER and AUTHORITY verdicts, ranked BUYER first, each with a one-line reason, rendering each surfaced thread as a clickable markdown link `[title](url)` from the candidate's `url` verbatim. Render the result as the two-table layout in run Step 4 (a BUYER SIGNALS table, then an AUTHORITY PLAYS table). Cap at this mode's `pattern_caps`. On zero judged surfaces, print the empty-state ladder from run's "After completion". Notion `Pattern` = `rivals`, emoji prefix `🥷`.
