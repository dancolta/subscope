---
name: subscope-build-vs-buy
description: Surface explicit build-vs-buy debate threads with numeric arguments (engineering hours, TCO, payback). OP is rationalizing the decision publicly — your worldview is the answer. Triggers on "build vs buy", "/subscope-build-vs-buy", "find build-vs-buy debates", "in-house vs SaaS", "make-or-buy decisions".
allowed-tools: Bash, Read, Write
---

# /subscope-build-vs-buy (⚖️)

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m subscope.cli fetch-score --mode build-vs-buy --candidates
```

The engine searches each configured sub for in-house-vs-SaaS debate language across the recent window via keyless Reddit search, then hands you the candidates. Cooling queue applies.

Judge every entry in `candidates[]` against the user's offer with the offer-relevance judge in `skills/run/SKILL.md` Step 3.5 (load offer.yml, example-pains.yml, brand-anchor.yml, keywords.yml once). A real build-vs-buy thread, where the OP is rationalizing the decision publicly, is a strong BUYER or AUTHORITY signal. Surface only BUYER and AUTHORITY verdicts, ranked BUYER first, each with a one-line reason, rendering each surfaced thread as a clickable markdown link `[title](url)` from the candidate's `url` verbatim. Render the result as the two-table layout in run Step 4 (a BUYER SIGNALS table, then an AUTHORITY PLAYS table). Cap at this mode's `pattern_caps`. On zero judged surfaces, print the empty-state ladder from run's "After completion". Notion `Pattern` = `build-vs-buy`, emoji prefix `⚖️`.
