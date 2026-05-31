---
name: subscope-churn
description: Surface high-intent Reddit posts where someone explicitly says they are canceling, switching from, or fed up with a named SaaS vendor. Pure buying intent. Triggers on "churn signals", "/subscope-churn", "find churn posts", "who's canceling", "switching from posts", "churn-signals scan".
allowed-tools: Bash, Read, Write
---

# /subscope-churn (⚡)

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m subscope.cli fetch-score --mode churn --candidates
```

The engine searches each configured sub for switching and cancellation language ("canceling Apollo", "switching from HubSpot") across the recent window via keyless Reddit search, then hands you the candidates. Cooling queue still applies.

Judge every entry in `candidates[]` against the user's offer with the offer-relevance judge in `skills/run/SKILL.md` Step 3.5 (load offer.yml, example-pains.yml, brand-anchor.yml, keywords.yml once). Surface only BUYER and AUTHORITY verdicts, ranked BUYER first, each with a one-line reason, rendering each surfaced thread as a clickable markdown link `[title](url)` from the candidate's `url` verbatim. Render the result as the two-table layout in run Step 4 (a BUYER SIGNALS table, then an AUTHORITY PLAYS table). Cap at this mode's `pattern_caps`. On zero judged surfaces, print the empty-state ladder from run's "After completion". Notion `Pattern` = `churn`, emoji prefix `⚡`.
