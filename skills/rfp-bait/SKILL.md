---
name: subscope-rfp-bait
description: Surface "X vs Y vs Z" comparison threads where ≥2 vendors are named in a comparative structure. Adding a 4th non-cliche option is welcomed, not seen as spam. Triggers on "rfp bait", "/subscope-rfp-bait", "find comparison threads", "vs threads", "shortlist threads", "evaluation threads".
allowed-tools: Bash, Read, Write
---

# /subscope-rfp-bait (🤝)

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m subscope.cli fetch-score --mode rfp-bait --candidates
```

The engine searches each configured sub for comparison-shaped threads ("vs", "versus", "between", "shortlist") across the recent window via keyless Reddit search, then hands you the candidates. Cooling queue applies.

Judge every entry in `candidates[]` against the user's offer with the offer-relevance judge in `skills/run/SKILL.md` Step 3.5 (load offer.yml, example-pains.yml, brand-anchor.yml, keywords.yml once). A thread naming two or more vendors in a comparative structure is the target. Surface only BUYER and AUTHORITY verdicts, ranked BUYER first, each with a one-line reason, rendering each surfaced thread as a clickable markdown link `[title](url)` from the candidate's `url` verbatim. Render the result as the two-table layout in run Step 4 (a BUYER SIGNALS table, then an AUTHORITY PLAYS table). Cap at this mode's `pattern_caps`. On zero judged surfaces, print the empty-state ladder from run's "After completion". Notion `Pattern` = `rfp-bait`, emoji prefix `🤝`.
