---
name: subscope-pricing-rage
description: Surface Reddit price-hike rage threads (Salesforce/HubSpot/Gong cyclical Q1/Q3 spikes). Time-sensitive — cooling queue auto-disabled. Triggers on "pricing rage", "/subscope-pricing-rage", "find price hike threads", "renewal complaints", "predatory pricing posts", "tier change rants".
allowed-tools: Bash, Read, Write
---

# /subscope-pricing-rage (🔥)

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m subscope.cli fetch-score --mode pricing-rage --candidates
```

**Cooling queue auto-disabled for this mode.** Price-hike threads spike fast and decay fast, so surfaces land state=hot immediately. The engine searches each configured sub for price-hike and renewal language across the last 7 days (keyless Reddit search), then hands you the candidates.

Judge every entry in `candidates[]` against the user's offer with the offer-relevance judge in `skills/run/SKILL.md` Step 3.5 (load offer.yml, example-pains.yml, brand-anchor.yml, keywords.yml once). Surface only BUYER and AUTHORITY verdicts, ranked BUYER first, each with a one-line reason, rendering each surfaced thread as a clickable markdown link `[title](url)` from the candidate's `url` verbatim (never hand-compose a Reddit URL). Render the result as the two-table layout in run Step 4 (a BUYER SIGNALS table, then an AUTHORITY PLAYS table). Cap at this mode's `pattern_caps`. On zero judged surfaces, print the empty-state ladder from run's "After completion". Notion `Pattern` = `pricing-rage`, emoji prefix `🔥`.
