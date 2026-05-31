---
name: subscope-resurrect
description: Find 6-18 month old high-quality Reddit threads that still get Google traffic. Late comments compound forever via SEO. Triggers on "resurrect threads", "/subscope-resurrect", "find old threads worth commenting", "SEO comment opportunities", "thread resurrect".
allowed-tools: Bash, Read, Write
---

# /subscope-resurrect (🪦)

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m subscope.cli fetch-score --mode resurrect --candidates
```

**Behavior diverges from other modes.** Instead of `/r/<sub>/new`, this mode searches each configured sub via Reddit's keyless search feed (`sort=top, t=year`, widened to `t=all` for the 12-18 month tail) and keeps only threads aged 6 to 18 months, using a client-side age band. It does NOT read `/new` and does NOT fall back to `/new`: a 3-to-4-day `/new` window cannot contain a 6-18 month thread, so a reachable sub with no in-window thread simply contributes nothing this run. Scoring uses `config/weights-resurrect.yml` so freshness decay does not zero out an old thread.

Cooling queue applies (these are evergreen, not time-sensitive).

Judge every entry in `candidates[]` against the user's offer with the offer-relevance judge in `skills/run/SKILL.md` Step 3.5 (load offer.yml, example-pains.yml, brand-anchor.yml, keywords.yml once). A high-quality older thread that still ranks and pulls Google traffic, where a late helpful reply compounds via SEO, is the target. Surface only BUYER and AUTHORITY verdicts, each with a one-line reason, rendering the result as the two-table layout in run Step 4 (a BUYER SIGNALS table, then an AUTHORITY PLAYS table, with hyperlinked subreddit and thread). Cap at this mode's `pattern_caps` (default 3). On zero judged surfaces, say so plainly and point to widening the sub list. Notion `Pattern` = `resurrect`, emoji prefix `🪦`.
