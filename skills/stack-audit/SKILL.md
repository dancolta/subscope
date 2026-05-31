---
name: subscope-stack-audit
description: Surface Reddit threads where an OP publicly lists 8+ tools in their stack and asks how to consolidate. Highest-intent format — OP is already in cutting mode, lurkers are watching. Triggers on "stack audit", "/subscope-stack-audit", "stack rationalization", "find stack consolidation threads", "tool sprawl posts", "consolidation play".
allowed-tools: Bash, Read, Write
---

# /subscope-stack-audit (🧱)

Run the daily surface in **stack-audit mode**, tuned for OPs publicly listing many SaaS tools and asking how to consolidate.

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m subscope.cli fetch-score --mode stack-audit --candidates
```

The engine searches each configured sub for stack-consolidation language (stack, tool sprawl, consolidate, "what can I cut") across the recent window via keyless Reddit search, then hands you the candidates. Surfaces land in the cooling queue (30 min hold) like the default run.

Judge every entry in `candidates[]` against the user's offer with the offer-relevance judge in `skills/run/SKILL.md` Step 3.5 (load offer.yml, example-pains.yml, brand-anchor.yml, keywords.yml once). A thread where the OP lists many tools and asks what to cut is the highest-intent format. Surface only BUYER and AUTHORITY verdicts, ranked BUYER first, each with a one-line reason, rendering each surfaced thread as a clickable markdown link `[title](url)` from the candidate's `url` verbatim. Render the result as the two-table layout in run Step 4 (a BUYER SIGNALS table, then an AUTHORITY PLAYS table). Cap at this mode's `pattern_caps`. On zero judged surfaces, print the empty-state ladder from run's "After completion". Optional Notion sync writes the `Pattern` column = `stack-audit` (emoji prefix `🧱`); see `skills/run/SKILL.md` for the Notion procedure. No drafting.
