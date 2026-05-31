---
name: subscope-run
description: Run the daily Reddit pain-post surface. Fetch new posts from configured subs, score by intent/keyword/freshness, optionally classify via Claude Haiku, dedup against history, hold in cooling queue, and emit inline markdown (plus optional Notion sync). Triggers on "run subscope", "/subscope run", "daily reddit", "scan reddit", "show today's reddit posts", or the default `/subscope-run` invocation.
allowed-tools: Bash, Read, Write
---

# /subscope-run

Daily Reddit surfacing orchestrator. Python (under `engine/`) does fetch + gate + score + SQLite + JSON output. This skill is the Claude-side wrapper: it invokes the engine, optionally syncs to Notion (if configured), and prints the inline list to chat.

## Preflight

1. Check whether user has personalized targeting at `~/.config/subscope/subreddits.yml`. If missing, the engine still runs using bundled generic defaults, but results will be off-target. Recommend `/subscope-onboard` (one conversation, ~5 min, includes the first scan) with a one-line nudge:
   `(no personal targeting found, scanning with generic defaults. /subscope-onboard for sharper results.)`
   Proceed with the run unless user explicitly cancels.

## Daily run procedure

### Step 1: Fetch + candidates (Python engine)

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m subscope.cli fetch-score --candidates
```

Engine output: a single JSON document on stdout with `run_id`, `status`, `fetched`, `surfaced`, `buyer_count`, `authority_count`, `subs_skipped_rate_limit`, `fetch_stats`, `dropped_counts`, `surfaces[]`, `inline_table`, `inline_markdown`, and (with `--candidates`) a `candidates[]` array plus `candidate_count` / `candidate_total`.

`candidates[]` is the recall pre-filter: every fetched post that cleared the absolute rejects (NSFW, removed/locked, vendor-spam, off-topic-sub, tier-3), each with deterministic features: `title`, `body`, `url`, `sub`, `tier`, `age_h`, `kw_hits`, `matched_kw`, `names_brand` (matched against the user's `brand_anchor`), `question_intent`, `pain_intent`, `engagement_available`, `soft_reason`. The engine deliberately does NOT decide relevance for these. **You do, in Step 3.5.** The engine's own `surfaces[]`/`inline_table` are the older lexical-gate output; under judge-first they are a secondary signal, not the chat output.

The `status` field tells you why a run produced few or no surfaces, so you never show the wrong message:
- `status: "ok"` and `surfaced > 0` -> normal run, render the table (Step 4).
- `status: "ok"` and `surfaced == 0` -> Reddit was reachable, today was just quiet. Show the EMPTY-DAY copy below.
- `status: "rate_limited"` -> Reddit rate-limited this run (HTTP 429). Some subreddits may have been skipped (`subs_skipped_rate_limit` says how many), so the list can be partial or empty. This is transient. Render any surfaces you DID get, then show the RATE-LIMITED copy below. Do NOT call this "blocked".
- `status: "blocked"` -> every Reddit feed request failed for a non-rate-limit reason (Reddit's edge returned 403, or the network was down). The scan could not read anything. Show the BLOCKED copy below.

**Dual-track surfaces.** Results are split into two tracks. `surfaced` is the combined total; `buyer_count` and `authority_count` break it down. Every entry in `surfaces[]` carries a `track` field:
- `track: "buyer"` = Buyer signals. The post names a specific tool or brand AND shows buying intent. A reply here moves a deal.
- `track: "authority"` = Authority plays. On-topic, answerable question with no buyer present yet. A reply here builds presence and credibility, it does not close a sale.

The `inline_markdown` and `inline_table` fields already render both tracks as two labeled sections (BUYER SIGNALS first, then AUTHORITY PLAYS). When the authority track is empty, only the buyer section renders. The authority track can be toggled in `weights.yml` under `authority_track.enabled`. When disabled, the run reverts to buyer-only output exactly.

### Step 2: Optional Notion sync

Read `~/.config/subscope/notion.yml`. Branch on the `mode` field:

**Branch A: `mode: mcp` (recommended path, auth via Notion MCP)**

1. Probe for any `mcp__*notion*` tool. If absent, print one line:
   `(Notion MCP not connected, skipping sync. Install with: claude mcp add --transport http notion https://mcp.notion.com/mcp)` and skip to Step 4.
2. Resolve the database ID by calling the MCP `notion-search` tool with `query=<database_name from notion.yml>`. If multiple matches, prefer one with `object: database`. If no match, print `(Notion DB "<name>" not found, skipping sync)` and skip to Step 4.
3. For each surface in the engine output, call the MCP `notion-create-pages` tool with `parent={"database_id": <resolved_id>}` and the property map below.
4. Sync failure is **non-fatal**. Capture the reason and proceed.

**Branch B: `api_key` + `database_id` (legacy SDK path)**

1. For each surface, create a row in the configured Notion database via the Notion REST API using the api_key + database_id from notion.yml.
2. Sync failure is **non-fatal**.

**Property map (both branches):**

- `Title`, `Tier`, `Subreddit`, `Score`, `Upvotes`, `Comments`, `Posted` (ISO date), `Pain`, `Fit`, `URL` (verbatim from engine output, **never hand-compose Reddit URLs**), `Surfaced on` (today, ISO)
- `Pattern` = the mode that produced the surface (default: `run`)
- `Track` ← read from `surface.track` (`buyer` or `authority`). Buyer = a reply moves a deal; authority = a reply builds presence, no buyer yet. Skip this property if the Notion DB has no `Track` column (it is optional, added with dual-track surfaces).
- `State` = `Drafting` if cooling queue active, else `Hot`
- `OP score` ← read from `surface.op_score` (string like `"2y old · 4.2k karma · 12% wrong-audience"`)

If `notion.yml` is missing, skip silently.

### Step 3: Optional Slack push (handled by Python automatically)

If `~/.config/subscope/slack.json` exists OR `SLACK_WEBHOOK_URL` env is set, the engine pushes a formatted message to that webhook at the end of `fetch-score`. This skill does NOT need to do anything. The integration is in `engine/subscope/lib/slack.py` and silently no-ops if no webhook is configured. To suppress for one run, pass `--no-slack` to `fetch-score`.

### Step 3.5: Offer-relevance judge (THIS is the surfacing decision)

Under judge-first, **you** decide what surfaces by judging each candidate against the user's offer. The engine's lexical gate is only a recall pre-filter; never rely on keyword density for precision. This step is what guarantees a first-run user does not get spammy or off-topic results.

**Load the offer context once** (read whichever exist):
- `~/.config/subscope/offer.yml` (if present: `what_offering`, `who_to_reach`, `pain`)
- `~/.config/subscope/example-pains.yml` (pain posts in the ICP's voice, a strong offer proxy)
- `~/.config/subscope/brand-anchor.yml` (competitors / tools the ICP touches)
- `~/.config/subscope/keywords.yml` (category terms)

From those, hold a one-paragraph offer model in mind: WHAT they sell, WHO buys it, and the PAIN that signals a buyer.

**Judge every candidate** in `candidates[]`. Assign exactly one verdict:

- **BUYER**: the poster is plausibly a buyer for the CORE offer: they have the pain the offer solves, or they are evaluating, switching, or pricing tools in the offer's category, and a reply could move a deal. Example: "moving off Dentrix, anyone on a cloud PMS?"
- **AUTHORITY**: a real ICP person with an on-topic problem who is NOT a direct buyer for the core offer: adjacent tools, industry ops questions, where a helpful reply builds credibility, not a sale. Example: a lab asking about imaging export, a practice asking who builds dental websites.
- **REJECT**: everything else: patient/clinical questions, careers/jobs, personal finance, vendor self-promo or spam, anything off-topic. **When in doubt, REJECT.**

**Hard rules (this is what keeps the list clean):**
- Judge against the OFFER, not keywords. A post that names a brand or hits a keyword but is a patient clinical question (or a job post, or spam) is REJECT. `names_brand: true` is NOT a pass.
- A first run must never surface spam or off-topic posts. If a row would make the user think "why am I seeing this?", it is REJECT.
- Every BUYER and AUTHORITY surface MUST carry a one-line reason naming the offer-pain or signal it matched. No defensible reason means do not surface it.
- Precision over volume. Surfacing 2 right posts beats surfacing 8 with 3 wrong ones. There is no minimum.
- Cap total surfaces at the daily cap (`weights.yml` `daily_output`, default ~10). Rank BUYER above AUTHORITY, then by strength of buying intent.
- Every surfaced thread MUST be rendered as a clickable link to its Reddit thread, using the candidate's `url` verbatim (read it from the JSON; NEVER hand-compose a Reddit URL). A surfaced row without its clickable link is a defect.

This judge is the same decision the optional `classify.py` path makes headlessly when a user supplies an LLM key; here you are the judge because the Claude session is the LLM.

### Step 4: Output (surface preference aware)

Read `~/.config/subscope/surface.yml` if it exists:

```yaml
modes: [table]            # or [notion], [table, notion], []
default_render: table     # which surface /subscope-run prints first in chat
```

Rendering rules:

- If `surface.yml` is missing → default to `modes: [table]`.
- If `modes` contains `table` → render the judged output as **two separate markdown tables**, the `BUYER SIGNALS` table first, then the `AUTHORITY PLAYS` table. Each table uses exactly these columns:

  ```
  ### BUYER SIGNALS
  | Subreddit | Thread | Why it matters | Age |
  |---|---|---|---|
  | [r/<sub>](https://www.reddit.com/r/<sub>) | [<post title>](<url>) | <one-line reason> | <age> |
  ```

  Cell rules:
  - **Subreddit**: a clickable link `[r/<sub>](https://www.reddit.com/r/<sub>)`; the subreddit name is the link text.
  - **Thread**: the post title as a clickable link using the candidate `url` **verbatim** from the JSON. NEVER hand-compose a Reddit URL (one wrong character can resolve to an unrelated, possibly NSFW post). Trim a very long title to ~70 chars but keep the link intact, and escape any `|` in the title as `\|`.
  - **Why it matters**: one short phrase on why this post fits the offer (the judge reason). A phrase, not a sentence.
  - **Age**: from the candidate `age_h`, hours when under 24h (e.g. `14h`), else days (e.g. `6d`).

  Omit a table entirely when its track has no surfaces. This two-table layout, not the engine's `inline_table`, is the chat surface under judge-first. (`inline_table` remains in the JSON as the lexical-gate fallback.)
- If `modes` contains `notion` → do the Notion sync above. If the user picked BOTH `table` and `notion`, render the table in chat AND sync to Notion.
- If `modes` is empty `[]` → don't print anything beyond JSON (for piping).

The `inline_markdown` field stays in the JSON for backwards compatibility (the older verbose format). Prefer `inline_table` for chat unless the user explicitly asks for the long form.

If Notion sync was attempted and failed, append exactly one line: `(Notion sync failed: <reason>)`.

## Critical guardrails

- **No em dashes** in any output written to Notion or chat. The Python output is em-dash-free; preserve that.
- **NEVER hand-compose Reddit URLs.** Read `url` directly from the JSON. Post IDs are globally unique; a single typo can resolve to a completely unrelated (potentially NSFW) post.
- **Idempotent:** running twice surfaces zero new posts (`surfaced.post_id` PK in SQLite enforces this).
- **No drafting in v1.** If the user asks "draft a reply for post N", politely defer, that is a deliberate omission, see PLAN.md §6.

## Configuration

All configs live under `${SUBSCOPE_CONFIG:-~/.config/subscope/}`:

| File | Purpose |
|---|---|
| `llm.json` | LLM provider preference |
| `subreddits.yml` | Active sub list, copied from a preset at setup |
| `keywords.yml` | Active keyword bucket |
| `weights.yml` | Score weights + gate thresholds |
| `notion.yml` (optional) | Notion DB ID + integration |
| `obsidian.yml` (optional) | Vault path for pulse digests |
| `dataforseo.yml` (optional) | DataForSEO credentials |
| `firecrawl.yml` (optional) | Firecrawl API key |

State (SQLite, logs) lives under `${SUBSCOPE_DATA:-~/.local/share/subscope/}`.

## After completion

Branch on the engine's `status` field (read it from the JSON):

**`status: "ok"` and the judge surfaced >= 1** (the normal case): render the two judged sections per Step 4. Do NOT add a preamble. If Notion sync was attempted and failed, append exactly one line noting the reason.

**`status: "ok"` and the judge surfaced 0** (Reddit was reachable, you judged every candidate REJECT): NEVER print a bare "nothing found". Print the empty-state LADDER so the user always sees the run did real work and what to do next:

```
Scanned <fetched> posts across <the subs scanned>. None cleared the buyer or authority bar today.

Closest <=3 (you judge if they are worth a glance):
  - <title> (r/sub): <why it was on-topic but did not clear the bar>

Widen options:
  - run again later, fresh posts land through the day
  - /subscope-profile to add a subreddit or loosen targeting
  - broader cross-subreddit keyword search is coming in a future update
```

Build the "Closest" list from the highest-ranked candidates you judged REJECT-but-near (on-topic adjacent, not clinical/career/spam). If there were literally zero on-topic candidates (e.g. the subs were all clinical/patient noise that day), say that plainly in one line and point to `/subscope-profile`, and skip the "Closest" block. This empty-state is a feature, not an error: a clear niche must never see a dead end.

**`status: "rate_limited"`** (Reddit returned HTTP 429 this run): if there ARE surfaces, render them first per Step 4 (they are real, just a partial list). Then print this copy verbatim:

```
Reddit rate-limited this run, so some subreddits were skipped. Any posts above are real, the list is just partial.

This is temporary, not a block. subscope reads Reddit's public RSS feeds with no login or API key. Reddit caps how fast a single machine can pull feeds, and this run hit that cap. Run /subscope-run again in a minute to pick up the rest.
```

Do NOT call this "blocked" and do NOT suggest any Reddit login, API key, or OAuth setup. Re-running shortly is the fix.

**`status: "blocked"`** (every Reddit feed request failed): print this copy verbatim, nothing else:

```
Could not read Reddit this run. Every feed request was blocked or unreachable.

What this is NOT: this is not a setup or credentials problem. subscope uses Reddit's public RSS feeds and needs no login, no API key, and no account.

Likely causes:
  - a temporary network issue on this machine (check your connection, then re-run)
  - Reddit's edge throttling this IP for a short window (wait a few minutes, then re-run)

If it keeps failing across several runs over a day, open an issue: github.com/dancolta/subscope/issues
```

Do NOT, under any circumstances, tell the user to configure Reddit OAuth, add a Reddit API key, or run a Reddit auth setup step. No such step exists. The fetch path is keyless RSS by design.
