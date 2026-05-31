---
name: subscope-onboard
description: Mandatory first-run setup for subscope. One conversation, three plain questions, one confirmation, optional integrations, first scan. Paste URLs, answer what-you-sell / who-buys-it / what's-the-pain, confirm the targeting card, pick integrations to connect (DataForSEO, Firecrawl, Notion, Slack, Obsidian), scan. No fast path. Every install passes through this. Triggers on "onboard", "/subscope-onboard", "set up subscope", "first time setup", "configure subscope", "get started with subscope", "install subscope".
allowed-tools: Bash, Read, Write, Edit, WebFetch
---

# /subscope-onboard

First-run setup. Seven turns, plain questions, one confirmation, optional integrations, first scan.

## Operating principles

1. **Ask, do not infer-and-confirm.** The user tells you what they sell, who buys it, what the pain is. Do not show an 8-field form for them to audit.
2. **One thing per turn.** Each chat message asks for exactly one input or shows exactly one summary.
3. **WebFetch silently in the background.** While the user answers turn 2, 3, 4 you are scraping their URLs. Never narrate this.
4. **Integrations are optional, not deferred.** T6 offers DataForSEO, Firecrawl, Notion, Slack, Obsidian as a single menu. Skip is a first-class choice.
5. **Verify creds inline.** If a paste fails, re-ask once. Twice failed, log and move on. The scan still runs.
6. **No filler.** No "welcome", "let's get started", "great", "perfect". No exclamation marks. No em dashes anywhere.

## Turn 1: collect URLs

Print verbatim:

```
SUBSCOPE ONBOARDING  ·  1 / 7
─────────────────────────────

I'll use these URLs to seed your Reddit targeting profile.
Paste the following:

→  Homepage URL
→  Case studies   (optional)
→  Blog / pricing (optional)

One per line.
```

Wait for input. Accept 1 to N URLs. Warn if more than 8.

Kick off WebFetch on all provided URLs **in parallel and in the background**. Extract:
- H1 / sub-headline / positioning line
- Linked case studies and pricing pages
- Visible competitor names ("alternative to X", "replace Y")
- Pain phrasing from problem statements
- Buyer titles quoted in case studies

Save raw fetch output to `~/.config/subscope/.onboard-draft.json` as you go.

**Background warmup.** As soon as the user pastes URLs, kick off the enrichment warmup in parallel with WebFetch so the DataForSEO competitor list + Firecrawl homepage scrape are cached by the time we reach T5 discovery. Silent no-op if DFS/Firecrawl keys are absent. Substitute `$HOMEPAGE_URL` with the first pasted URL:

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -c "
from subscope.lib import enrich, store
with store.connect() as conn:
    enrich.warmup_for_onboarding('$HOMEPAGE_URL', conn)
" &
```

Do not block on this. Do not show any status, recap, or summary. Move straight to T2.

## Turn 2: what do you sell

Print verbatim:

```
SUBSCOPE ONBOARDING  ·  2 / 7
─────────────────────────────

What do you sell?

→  One line is enough.
→  Example: "Reddit lead-gen for B2B SaaS."
```

Wait for input. Save to scratchpad. No echo, no confirmation. Move to T3.

## Turn 3: who buys it

Print verbatim:

```
SUBSCOPE ONBOARDING  ·  3 / 7
─────────────────────────────

Who buys it?

→  A job title works.
→  Example: "Head of Ops at a SaaS startup."
→  Or just: "RevOps leads."
```

Wait for input. Save to scratchpad. Move to T4.

## Turn 4: what is the pain

Print verbatim:

```
SUBSCOPE ONBOARDING  ·  4 / 7
─────────────────────────────

What do they complain about right before they find you?

→  A real customer quote is gold.
→  Paraphrase is fine.
```

Wait for input. Save to scratchpad.

**Run live subreddit discovery before T5.** This calls the engine to find subs where the user's pain phrasing is actively discussed on Reddit. Replaces the old templatish archetype-map seed.

Pipe the answers JSON via stdin so apostrophes / quotes in user input don't break shell quoting. Pass any competitor brands you found in the WebFetch (T1) via `--competitors` as a comma-separated list. These are critical: they generate "replacing X" and "X alternative" queries which dramatically improve sub relevance for Reddit discovery.

```bash
cd "$CLAUDE_PLUGIN_ROOT" && python3 -c "
import json
print(json.dumps({
    'what_offering': '''<T2_VALUE>''',
    'who_to_reach':  '''<T3_VALUE>''',
    'pain_quote':    '''<T4_VALUE>''',
}))" | PYTHONPATH=engine python3 -m subscope.cli discover \
  --homepage "$HOMEPAGE_URL" \
  --competitors "<COMMA_SEPARATED_COMPETITOR_BRANDS>" \
  --skip-validation \
  --answers-json -
```

`--skip-validation` keeps onboarding light: it returns the candidate subs plus a relative `relevance` score (0-100) WITHOUT the per-sub freshness fetches. Onboarding's job is to confirm the sub NAMES; the real `/subscope-run` scan at T7 validates by actually surfacing. This also means fewer Reddit calls during setup.

Substitute:
- `<T2_VALUE>`, `<T3_VALUE>`, `<T4_VALUE>` with the exact answers from the scratchpad
- `<COMMA_SEPARATED_COMPETITOR_BRANDS>` with whatever brands Claude extracted from the user's homepage/case-studies during T1 WebFetch (e.g. `"Zapier,n8n,Make,Bill.com,Apollo.io"`). If no brands were found, pass an empty string.

The triple-quoted strings handle embedded apostrophes safely; `--answers-json -` reads the piped JSON from stdin.

The engine (run with `--skip-validation`) returns JSON with these fields:
- `subs`: ranked candidate subs. Each entry has `name`, `relevance` (0-100, relative match-strength to the offer), `why` (plain-English why-chosen line), `relevance_path` ("competitor"/"noun"), `thread_count`, `sources`, `noise_downranked`, `validation_skipped: true`. (No per-sub freshness fields: validation is deferred to the first scan at T7.)
- `needs_clarification`, `clarifier_reason` (`no_candidates`), `clarifier_prompt`, `discovery_unreachable`, `phase_a_count`, `validation_skipped`

The `relevance` score is a topical match estimate (how strongly the sub's threads match the user's competitors + offer vocabulary), not a verified-buyer guarantee. The first `/subscope-run` at T7 is what proves a sub, by actually surfacing.

**MANDATORY relevance review (precision step, do not skip).** The engine is the recall stage: it surfaces every candidate above the floor, matched lexically. Lexical matching cannot tell a real buyer's community from a same-shaped one. Before rendering the T5 card, review each candidate's `name` + `why` against THIS user's offer and DROP the semantic false positives:

- KEEP: communities where people shop or compare tools, vent about a named competitor, or ask which software to buy in the user's category.
- DROP: career / "should I switch professions" subs (e.g. a general careers thread for a dental-software seller), self-promotion hubs, brand-name collisions (the law tool "Clio" vs the Renault "Clio", "Anchor" the boat), and subs whose only match is an unrelated meaning of a product word.
- When in doubt, keep a sub only if a real buyer for this product would plausibly post there. Drop silently; do not show dropped candidates to the user.

After the review, you have the final sub list. If the review leaves fewer than 3 subs, treat it like a thin result (offer the clarifier / broaden). Never pad with generic founder subs.

**If `needs_clarification` is true** (under `--skip-validation` this is `no_candidates`: Phase A found nothing relevant): render the T4.5 sub-turn with the engine's `clarifier_prompt` verbatim, asking for the vertical. Wait for the reply, then re-run discover with `--vertical "<user reply>"` (keep `--skip-validation`). Ask only once; use the second result regardless.

**If `discovery_unreachable` is true:** the T5 card falls back to the archetype starter set (see the card rules below).

## Turn 5: confirm the subreddits

Build a short confirmation card from the three answers + the discovery result. Keep it tight: the user confirms sub NAMES and the offer summary, not a scan. Render this shape:

```
SUBSCOPE ONBOARDING  ·  5 / 7
─────────────────────────────

Here's what I'll watch. Confirm or correct.

You sell       <one-liner from T2>
Buyers         <titles from T3>
Pain pattern   <theme from T4>

Subreddits  (relevance to your offer, 0-100)
  [<rel>] r/<sub>   <short why, up to 45 chars>
  [<rel>] r/<sub>   <short why>
  [<rel>] r/<sub>   <short why>

I also read beyond these. Every /subscope-run judges each post against your
offer, so a strong buyer in a sub that is not listed here can still surface.

Competitors    <up to 6, inferred from WebFetch + DFS cache>

Reply "go" to confirm, or tell me what to fix (add or drop a sub, edit a detail).
```

Rendering rules:
- One row per sub in `subs`, sorted by `relevance` descending. `[<rel>]` is the integer `relevance` right-padded to 3 (e.g. `[ 92]`, `[ 60]`).
- `<short why>` comes from each sub's `why`, truncated to 45 chars. Omit it if empty; never invent one.
- Always include the "I also read beyond these" line verbatim. It is the broader-audience disclaimer: subscope is not limited to the listed subs, the judge reads every post against the offer.
- Do NOT show freshness timestamps, evidence links, a dropped-subs line, or a confidence-band legend. Validation happens at the first scan (T7), not here.
- If `discovery_unreachable` is true, replace the Subreddits block with the archetype-map fallback and add one line: `(generic starting set, your first scan will refine it)`.
- If the final list (after the relevance review above) is short (1-3 subs), render what you have. Never pad with generic founder subs.

If the user replies with edits, apply silently and re-render the card. Do NOT re-render after a "go".

When the user says "go", proceed to T6.

## Turn 6: connect integrations

Print verbatim:

```
SUBSCOPE ONBOARDING  ·  6 / 7
─────────────────────────────

Connect anything before the scan?

→  dataforseo   Competitor keywords + search intent
→  firecrawl    Deeper URL crawling
→  notion       Daily digest in a Notion database
→  slack        Digest to a channel
→  obsidian     Weekly pulse in your vault

Reply with the ones you want, space-separated. Or "skip".
Example: "dataforseo notion" or "skip".
```

If user replies "skip", jump straight to T7. No marker file needed.

Otherwise parse the picks. For each picked integration, run its micro-prompt in order. Each one is its own short turn. Failed verification = re-ask once. Failed twice = log it, continue with the next pick.

**Per-sub-prompt skip path.** If at any sub-prompt the user replies "skip" (case-insensitive), drop that integration immediately, write a marker (`touch ~/.config/subscope/.<name>-skipped`), and move to the next picked integration without re-asking. If it was the last pick, jump straight to T7. Do not re-render the menu, do not ask for confirmation.

### dataforseo

Probe first: check session for any `mcp__dataforseo__*` tool. If present, skip the credential prompt and mark ready.

Otherwise print verbatim:

```
SUBSCOPE ONBOARDING  ·  6 / 7
─────────────────────────────
DataForSEO  (optional)

Get credentials at:
https://app.dataforseo.com/api-access

→  API password lives in the API Access tab.
→  Not your dashboard login.

Paste here:
login api_password

Full MCP toolset (optional):
claude mcp add dataforseo -e DATAFORSEO_USERNAME=<login> -e DATAFORSEO_PASSWORD=<pw> -- npx -y dataforseo-mcp-server

Or reply "skip".
```

On paste:

```bash
cd "$CLAUDE_PLUGIN_ROOT" && cat <<EOF | PYTHONPATH=engine python3 -m scripts.write_dataforseo_config
login: $DFS_LOGIN
password: $DFS_PASSWORD
EOF
```

Verify with a single live call:

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -c "
import base64, json, urllib.request, yaml
from pathlib import Path
cfg = yaml.safe_load(Path.home().joinpath('.config/subscope/dataforseo.yml').read_text())
auth = base64.b64encode(f\"{cfg['login']}:{cfg['password']}\".encode()).decode()
req = urllib.request.Request('https://api.dataforseo.com/v3/appendix/user_data', headers={'Authorization': f'Basic {auth}'})
try:
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    print('dataforseo: ok' if resp.get('status_code') == 20000 else f\"dataforseo: error {resp.get('status_message')}\")
except Exception as e:
    print(f'dataforseo: error {e}')
"
```

### firecrawl

Probe for `seo-firecrawl` skill or `FIRECRAWL_API_KEY` env var. If present, mark ready.

Otherwise print verbatim:

```
SUBSCOPE ONBOARDING  ·  6 / 7
─────────────────────────────
Firecrawl  (optional)

Paste your Firecrawl API key (starts with fc-).

→  Get one at: https://www.firecrawl.dev/app/api-keys

Full MCP (optional):
claude mcp add firecrawl -e FIRECRAWL_API_KEY=<key> -- npx -y firecrawl-mcp

Or reply "skip".
```

On paste:

```bash
cd "$CLAUDE_PLUGIN_ROOT" && cat <<EOF | PYTHONPATH=engine python3 -m scripts.write_firecrawl_config
api_key: $FIRECRAWL_API_KEY
EOF
```

Verify with a small scrape:

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -c "
import json, urllib.request, yaml
from pathlib import Path
cfg = yaml.safe_load(Path.home().joinpath('.config/subscope/firecrawl.yml').read_text())
req = urllib.request.Request(
    'https://api.firecrawl.dev/v1/scrape',
    data=json.dumps({'url': 'https://example.com', 'formats': ['markdown']}).encode(),
    headers={'Authorization': f\"Bearer {cfg['api_key']}\", 'Content-Type': 'application/json'},
)
try:
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    print('firecrawl: ok' if resp.get('success') else f\"firecrawl: error {resp}\")
except Exception as e:
    print(f'firecrawl: error {e}')
"
```

### notion

Probe for any `mcp__*notion*` tool. If present, skip the install step and jump to the database-name question.

Otherwise print verbatim:

```
SUBSCOPE ONBOARDING  ·  6 / 7
─────────────────────────────
Notion  (optional)

Install the official MCP, then authorize in your browser:
claude mcp add --transport http notion https://mcp.notion.com/mcp

Once OAuth completes, reply with the database name to write to.

Or reply "skip".
```

On reply with a database name, write the config:

```bash
cd "$CLAUDE_PLUGIN_ROOT" && cat <<EOF | PYTHONPATH=engine python3 -m scripts.write_notion_config
mode: mcp
database_name: $NOTION_DB_NAME
EOF
```

The engine resolves the database ID at runtime via the MCP `search` tool. No manual ID needed.

### slack

Print verbatim:

```
SUBSCOPE ONBOARDING  ·  6 / 7
─────────────────────────────
Slack  (optional)

Paste your Slack webhook URL.

→  Must start with https://hooks.slack.com/

Or reply "skip".
```

SSRF guard rejects anything that isn't `https://hooks.slack.com/...`. On valid paste:

```bash
cd "$CLAUDE_PLUGIN_ROOT" && cat <<EOF | PYTHONPATH=engine python3 -m scripts.write_slack_config
webhook_url: $SLACK_WEBHOOK_URL
EOF
```

### obsidian

Print verbatim:

```
SUBSCOPE ONBOARDING  ·  6 / 7
─────────────────────────────
Obsidian  (optional)

Paste your absolute Obsidian vault path.

Or reply "skip".
```

Verify the path exists:

```bash
[ -d "$VAULT_PATH" ] && echo "ok" || echo "missing"
```

If ok, write `~/.config/subscope/obsidian.yml`:

```yaml
vault_path: /absolute/path/to/vault
pulse_folder: subscope
```

### After all picks

Write the surface.yml with the chosen modes (always include `table` so chat output is on):

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m scripts.write_surface_config <<EOF
modes: [table, $CHOSEN_DESTINATIONS]
default_render: table
EOF
```

## Turn 7: write configs + run the first scan

Merge the targeting payload (scratchpad WebFetch + T2/T3/T4/T5 confirmed values) and write the YAML files:

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -c "
import json
from subscope.lib import profile_synth
payload = $PAYLOAD_JSON
files = profile_synth.to_yaml_files(payload)
written = profile_synth.write_to_xdg(files, backup=True)
for name, path in written.items():
    print(f'  wrote: {path}')
profile_synth.clear_draft('.onboard-draft.json')
"
```

Validate via `profile_synth.validate_synthesis(payload, weights_cfg)`. If validation fails, surface the failures and ask for a one-line correction.

Warm the enrichment cache once more as insurance. The same warmup fired in background at T1, but if the user finished onboarding faster than the API calls, or if they configured DFS/Firecrawl for the first time at T6, this second call fills the gap. Cached payloads short-circuit, so cost stays zero when T1's warmup already populated everything. Substitute `$HOMEPAGE_URL` with the URL the user pasted at T1:

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -c "
import json
from subscope.lib import enrich, store
with store.connect() as conn:
    result = enrich.warmup_for_onboarding('$HOMEPAGE_URL', conn)
print(json.dumps(result))
"
```

The output is informational only (showing which providers fired and what was cached). Do not surface it to the user unless both calls were attempted and both reported a fail-open `skipped_reason`, in which case mention once: "DataForSEO and Firecrawl could not reach their APIs. Cache will fill on the next successful onboarding."

Run the first scan (judge-first, same path as /subscope-run):

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m subscope.cli fetch-score --candidates
```

Branch on the engine's `status` field before rendering:

- `status: "ok"` with judged surfaces: judge the `candidates[]` per `skills/run/SKILL.md` Step 3.5 and render the output as the two-table layout in `run` Step 4 (a BUYER SIGNALS table, then an AUTHORITY PLAYS table, each with hyperlinked subreddit, hyperlinked thread, a short why, and age), not the raw `inline_table`. If destinations include Notion/Slack/Obsidian, the engine handles those automatically.
- `status: "ok"` with zero judged surfaces (the first scan read each sub's newest posts and nothing crossed the bar, or you judged every candidate REJECT): the targeting IS saved. A quiet first scan is normal for a focused niche, where switching threads are weekly, not daily. Do NOT auto-run anything else. Print verbatim, then WAIT for a yes/no reply:

  ```
  Your targeting is saved. This first scan read each sub's newest posts and nothing crossed the bar right now, which is normal for a focused niche.

  Want me to run a wider one-time scan? It searches the last 7 days across your subs instead of only the newest posts, so it usually finds something to prove the setup works. Reply "yes" to run it, or "no" to stop here and run /subscope-run later.
  ```

  If the user replies yes (or any clear affirmative), run the comprehensive 7-day scan below, judge its candidates with the SAME Step 3.5 judge, and render them as the two-table layout from `run` Step 4. This widens recall from "newest posts only" to "last 7 days via Reddit search", which is what proves the setup on a quiet niche:

  ```bash
  cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine python3 -m subscope.cli fetch-score --candidates --search --since-days 7 --window week --max-surfaces 12
  ```

  If this wider scan ALSO yields zero judged surfaces, print the empty-state ladder from `skills/run/SKILL.md` "After completion" (closest near-misses + `/subscope-profile` to widen the sub list). Never dead-end. If the user replies no, skip to the closing card below.

- `status: "rate_limited"`: the configs were written fine, Reddit just rate-limited this first scan (HTTP 429). Render any surfaces you got, then print verbatim:

  ```
  Configs saved. Reddit rate-limited this first scan, so some subreddits were skipped. This is temporary, not a block, and there is no login or API key to set up. Run /subscope-run again in a minute to pull your first full list.
  ```

  Do NOT call this "blocked" and do NOT suggest a Reddit login, API key, or OAuth setup.

- `status: "blocked"`: the configs were written fine, the scan just could not read Reddit this run. Print verbatim:

  ```
  Configs saved. Could not read Reddit on this first scan though, every feed request was blocked or unreachable.

  This is not a setup problem. subscope reads Reddit's public RSS feeds, so there is no login, API key, or account to configure. It is usually a temporary network or edge-throttle issue. Run /subscope-run in a few minutes to pull your first list.
  ```

  Do NOT tell the user to set up Reddit OAuth or a Reddit API key. There is no such step, the fetch path is keyless RSS by design.

After the scan output, print verbatim:

```
SUBSCOPE ONBOARDING  ·  7 / 7
─────────────────────────────
Done. Configs written to ~/.config/subscope/.

→  /subscope-run        Fresh scan
→  /subscope-tune       Sharpen the ranker after a few scans
→  /subscope-profile    Refine a single section
```

## Resumability

On invocation, check for `~/.config/subscope/.onboard-draft.json`:

- Present AND <24 hours old → ask: `Found a draft from earlier. Resume, or start fresh?`
- Present AND >24 hours old → delete, start fresh
- Absent → start fresh

The scratchpad records: URLs, WebFetch output, T2/T3/T4 answers, T5 confirmation, T6 picks. Resume lands at the unanswered turn.

The scratchpad is cleared on successful T7 config write.

## Anti-patterns

- **No exclamation marks.** Anywhere.
- **No em dashes.** Anywhere. Use commas, periods, or restructure.
- **No "welcome" / "let's get started" / "great" / "perfect".** Operational tone only.
- **No confidence bars, no progress bars.** Step counter in the header (`N / 7`) is the only allowed pacing signal. The user sees titled prompts, bulleted hints, and one summary card.
- **No 8-field confirmation form.** The three plain questions replace it.
- **No status recap lines.** WebFetch runs silent. The user sees questions, not engine narration.
- **Never re-render the T5 card after "go".** Once locked, move on.
- **Never block on a missing optional integration.** Failed twice = log, continue. The scan still runs.

## What's NOT in this skill

- LLM provider configuration. Power-user concern, kept in `/subscope-setup --llm`.
- Per-section refinement. After onboarding, `/subscope-profile` handles single-section deep dives.
- Preset shortcut. There is no preset shortcut. Every install passes through the full flow.
