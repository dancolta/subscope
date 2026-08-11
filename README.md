<div align="center">

# subscope

[![License: MIT](https://img.shields.io/github/license/dancolta/subscope?color=blue)](LICENSE) [![Release](https://img.shields.io/github/v/release/dancolta/subscope)](https://github.com/dancolta/subscope/releases) [![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-7c3aed)](https://docs.claude.com/en/docs/claude-code)

![subscope hero: a Claude Code chat after /subscope-run, showing two labeled sections, BUYER SIGNALS with ranked threads like "HubSpot renewal +28%, anyone moved off?" and AUTHORITY PLAYS with answerable threads, each tagged with subreddit, age, score, and pattern](assets/hero.gif)

**subscope reads Reddit for you and hands you the threads worth replying to: the people shopping for what you sell, and the questions you can answer to build authority.**

Run it whenever you want. Each scan returns 5 to 12 ranked threads in your Claude Code chat, split into two tracks:

**Buyer signals** · someone is shopping, a reply moves a deal<br>
**Authority plays** · no buyer yet, a reply builds credibility

You read them, decide where to jump in, and reply yourself: engage a potential buyer, or build your authority.

Keyless. No OAuth, no API key, no Reddit account. It reads Reddit's public RSS feeds. Free, MIT, local.

**It never posts for you.** Not an alert firehose, not a Reddit growth bot, not a $20 to $100 a month alert subscription. subscope finds the thread. You write the reply.

</div>

```bash
/plugin marketplace add dancolta/subscope   # register the marketplace (one time)
/plugin install subscope@subscope           # install the plugin
/subscope-onboard       # one-time targeting setup (~5 min), ends with your first scan
/subscope-run           # every scan after that
```

---

## What a scan returns

You run `/subscope-run`. A few seconds later, two ranked lists land in chat:

```
BUYER SIGNALS  ·  someone is shopping, a reply moves a deal
[T1] r/RevOps      14h · 92 · pricing-rage    "HubSpot renewal +28%, anyone moved off?"
[T1] r/SalesOps     6h · 88 · churn           "Canceling Apollo, what do you use instead?"
[T2] r/B2BSaaS      3h · 74 · alternative     "Alternative to Salesforce under 25 seats?"

AUTHORITY PLAYS  ·  no buyer yet, answer to build presence
[A]  r/Entrepreneur 5h · 61 · question        "How do you handle multi-entity invoicing?"
[A]  r/smallbusiness 9h · 58 · question       "Best way to track recurring client work?"
```

Each line is a live thread with a clickable link, ranked by how much your reply is worth and how fresh the window still is. Click in, read the room, write a comment that helps. That is the whole loop.

**Why two tracks.** Most tools stop at "someone mentioned your keyword." subscope splits the result. Buyer signals are demand you can capture now. Authority plays are the questions where showing up as the helpful expert compounds, builds the karma and history that make your buyer-signal replies land later, and keeps your account from looking like it only shows up to pitch.

---

## Who this is for

Anyone who sells anything. The onboarding adapts to your exact offer, you do not need to be a SaaS founder.

- **B2B SaaS** (per-seat, analytics, PM, HR): catch "30-person team, Notion is a mess, what's next?" while the OP is still reading replies.
- **Sales or marketing tools** (CRM, outreach): catch "Apollo just hiked us 40%, who's everyone moving to?" before the vendor pile-on hardens into a shortlist.
- **Agency or freelance** (marketing, RevOps, dev): catch "need a Webflow dev who knows e-commerce, 4 weeks" while you can still write something different from the eight identical pitches.
- **Developer tools** (API, framework, infra): catch "anyone moved off Temporal at scale?" while they are comparing, not after they have committed.

The edge is timing. subscope returns the thread while it is still forming, because you ran a scan early, not because anything was watching around the clock.

---

## The 8 signals it catches

Each pattern has its own scoring path. A `pricing-rage` thread and an `alternative-seeking` thread rank separately because they are different buying moments.

| Pattern | What it captures |
|---|---|
| `pricing-rage` | Public anger about a renewal hike |
| `churn` | "Looking to ditch X for..." threads |
| `build-vs-buy` | Debates with actual numbers attached |
| `rfp-bait` | A vs B vs C comparison threads |
| `stack-audit` | "Help me cut tools from my stack" posts |
| `alternative-seeking` | Explicit "alternative to X?" threads |
| `resurrect` | Quality threads aged 6 to 18 months still getting traffic |
| `rivals` | Any mention of a brand in your competitive set |

---

## What you do day to day

| Command | What it does |
|---|---|
| `/subscope-run` | Manual scan. 5 to 12 threads land in chat as Buyer signals + Authority plays, with pattern badges. Posts under 24 hours old get a freshness boost so first-mover threads surface even when the keyword gate barely catches them. |
| `/subscope-judge <n>` | Deeper read on a single thread. Returns the intent and a reply angle. It does not write the reply. |
| `/subscope-tune` | Mark surfaces good / bad / meh. The ranker back-propagates your judgments into per-sub weights and keyword scores, so the list sharpens to your niche the more you use it. |

---

## How it works

Three steps.

1. **Onboard once.** `/subscope-onboard` asks what you sell, who buys it, and what they complain about, then builds a targeting profile from your URLs, your competitor brands, and live Reddit threads matching your actual pain phrasing. This is mandatory first-run, there is no generic template.
2. **Run on demand.** `/subscope-run` fetches new posts from your subs, filters low-quality authors, ranks what is left, and returns the two-track list.
3. **You reply.** You open the threads, decide which are worth your time, and write the comment yourself on Reddit.

<details>
<summary>The onboarding flow in full (7 turns)</summary>

You will not see a single scored post until the flow ends. That is the tradeoff for targeting built around your offer instead of a SaaS-founder template.

1. **Paste URLs.** Homepage plus optional case studies, blog, or pricing. subscope scrapes them silently while you answer the next questions, pulling positioning, competitor names, buyer titles, and pain phrasing.
2. **What do you sell?** One line.
3. **Who buys it?** A job title is enough.
4. **What is the pain?** A real customer quote is gold. Paraphrase is fine.
5. **Confirm the targeting card.** What you sell, buyers, pain pattern, competitor list, and recommended subreddits, each with a relevance score. You are confirming the sub names; the first scan validates them by actually surfacing. Reply `go`, or tell the flow what to fix.
6. **Connect integrations (optional).** One menu: DataForSEO, Firecrawl, Notion, Slack, Obsidian. Reply `skip` for any or all.
7. **First scan runs.**

The flow writes config to `~/.config/subscope/` (subreddits, keywords, brand-anchor, example-pains). Refine later with `/subscope-profile`: redo the competitor anchor, rebuild pain language, swap a subreddit, just the section that drifted.

</details>

<details>
<summary>How subscope picks your subreddits</summary>

When you onboard, subscope does not hand you a generic list of r/SaaS and r/Entrepreneur. It searches Reddit for people discussing what you sell and scores each community by how well it fits your offer.

1. **Live search, not a template.** subscope searches Reddit in real time using your own pain phrasing and your competitors' brand names. Two people selling different things get different subreddits.
2. **A relevance score, and a real offer signal required.** Each candidate gets a 0-100 relevance score. A subreddit only makes the list if it carries a genuine offer signal: your category vocabulary, or one of your competitors named in its threads. Communities that merely share a generic word ("alternative", a pricing gripe) are dropped, not ranked.
3. **Claude reviews every candidate.** A keyword pass finds candidates, then Claude (running the plugin) reads each one and drops the false positives keyword matching cannot catch: career questions ("Software Engineering vs Dentistry"), self-promoters, and brand-name collisions (the law tool "Clio" vs the Renault "Clio").
4. **The list is where it looks first, not a fence.** Every `/subscope-run` judges each post against your offer, so a strong buyer in a community you did not list still surfaces. The first scan is what proves a sub, by actually surfacing one.

**Precision over volume, on purpose.** In a narrow niche you might see one to three subreddits, or an honest "no clearly relevant communities yet, what vertical are you in?" That beats padding the list with noise.

</details>

<details>
<summary>Under the hood: how a post gets surfaced</summary>

The Claude-side skills run the conversation. A small Python engine does the mechanical work, and it is deliberately keyless.

1. **Keyless fetch, with failover.** subscope reads Reddit's public RSS feeds. No OAuth, no API key, no account. If the primary Reddit host returns a 403 or a network blip, it fails over to a second Reddit host automatically, so one bad response does not kill the run. A genuine rate-limit (HTTP 429) is respected and reported, not worked around.
2. **A recall pre-filter, not a verdict.** The engine drops only the clear-cut rejects (NSFW, removed, vendor self-promo, off-topic subs) and passes everything else through as a candidate, tagged with plain features: which of your competitor brands it names, which of your keywords it hits, whether it reads as a question or a complaint, and how fresh it is.
3. **Claude judges each candidate against your offer.** This is the part that decides what you see. Claude reads every candidate against what you sell, who buys it, and the pain you described, then sorts each one into BUYER (a reply could move a deal), AUTHORITY (a helpful reply builds credibility, no buyer yet), or dropped. A post that only name-drops a brand but is really a patient or career question gets dropped. Every surfaced post carries a one-line reason. Keywords are the net that gathers candidates, not the thing that decides relevance.
4. **A quiet day is never a dead end.** When nothing clears the bar you get the count of what was scanned, the closest near-misses, and how to widen, not a blank "nothing found."

Everything runs on your machine. The engine writes only to local SQLite, sends nothing anywhere, and the JSON it emits could pipe into any orchestrator, not just Claude Code.

</details>

---

## subscope vs the alternatives

GummySearch was the go-to for Reddit buyer research. It shut down in November 2025. subscope is the free, keyless tool that does the high-intent part it was loved for.

|  | subscope | GummySearch | Syften / F5Bot | Manual |
|---|:---:|:---:|:---:|:---:|
| Price | Free, MIT | shut down 2025 (was $29 to $199/mo) | Syften from $19.95/mo, F5Bot free | Free |
| Keyless: no OAuth, no API key, no account | ✓ | ✗ | ✗ | partial |
| Ranked, author-vetted shortlist (not raw alerts) | ✓ | partial | ✗ | ✗ |
| Buyer-signal vs authority-play split | ✓ | ✗ | ✗ | ✗ |
| Human-in-the-loop, never auto-posts | ✓ | ✓ | ✓ | ✓ |
| Local-only data, zero telemetry | ✓ | ✗ | ✗ | ✓ |
| Runs inside Claude Code, reply in context | ✓ | ✗ | ✗ | ✗ |
| Freshness-boosted first-mover surfacing | ✓ | partial | partial | ✗ |

Pricing current as of May 2026: GummySearch closed Nov 30 2025, Syften plans run $19.95 to $99.95/mo, F5Bot's core is free. Verify before relying on competitor cells.

---

## FAQ

**What is subscope and what does it do?**
subscope is a free, open-source Claude Code plugin that finds Reddit threads where someone is actively shopping for a product or service. It reads public RSS feeds, scores posts by 8 buying-signal patterns, and returns 5 to 12 ranked threads in your Claude Code chat, split into buyer signals and authority plays. No API keys, no Reddit account, no auto-posting.

**How does subscope work without a Reddit API key?**
subscope is keyless by design. It reads Reddit's public RSS and Atom feeds instead of the authenticated JSON API, which Reddit edge-blocked for unauthenticated access. All processing happens locally in a SQLite database, with zero telemetry.

**Is subscope a GummySearch alternative?**
Yes. GummySearch, the go-to for Reddit buyer research, shut down on November 30, 2025. subscope is a free, open-source, keyless tool that covers the high-intent discovery GummySearch was used for, and it runs inside Claude Code instead of as a paid SaaS.

**How is subscope different from Syften or F5Bot?**
Syften is a paid alert service (plans from $19.95/mo) and F5Bot is a free keyword-alert tool. Both stop at "someone mentioned your keyword." subscope ranks threads by buying intent, vets the author, and splits results into buyer signals and authority plays, all keyless and local, instead of piping every match to your inbox.

**Does subscope post to Reddit automatically?**
No. subscope is strictly human-in-the-loop. It never auto-posts, never drafts replies, and has no account rotation. The plugin finds and ranks threads. You read them and write any reply yourself.

**What is the dual-track output?**
Every scan returns two ranked tracks. Buyer signals are threads where someone is shopping or switching tools, where a reply can move a deal. Authority plays are threads with no buyer intent yet, where answering the question builds credibility. Both pull from the same scored pool and are separated before output.

**What buying-signal patterns does subscope detect?**
Eight: pricing-rage, churn, build-vs-buy, rfp-bait, stack-audit, alternative-seeking, resurrect, and rivals. Each has its own scoring path so they rank independently.

---

<details>
<summary>Integrations (all optional)</summary>

subscope runs on day one with zero keys. Each integration is a silent no-op until you add it.

| Integration | Why | Setup |
|---|---|---|
| Bulk LLM grading | Grade posts at scale via any of 5 providers | One API key in setup |
| DataForSEO | SERP-verified competitor list for brand-anchor seeding | Paste login + API password |
| Firecrawl | Cleaner positioning extraction from your homepage | Paste `fc-...` key |
| Notion triage DB | Daily surfaces in a Notion database | ~5 min |
| Slack push | Morning digest to a channel | Paste one webhook URL |
| Obsidian digest | Weekly pulse via `/subscope-pulse` | Vault path in config |

**LLM providers for bulk grading:** Anthropic, OpenAI, Groq, OpenRouter, Ollama. Auto-detected from your key prefix. Without a key, the regex gate runs alone and `/subscope-judge` handles ad-hoc grading at no extra cost.

**DataForSEO + Firecrawl** are cached in SQLite (30 days for DFS, 90 for Firecrawl). Daily scans are cache-read only and never touch the network for enrichment. Disable per-run with `--no-enrich`, globally with `SUBSCOPE_NO_ENRICH=1`.

</details>

<details>
<summary>All 15 skills</summary>

The 3 core skills above are what you use day to day. The other 12 are setup, pattern-specific scans, and utilities.

**Setup and onboarding**

| Skill | What it does |
|---|---|
| `/subscope-setup` | Standalone config for LLM provider, surface choice, and dry-run validation. Most users do not need this; `/subscope-onboard` covers it. |
| `/subscope-onboard` | Mandatory first-run flow. Seven turns, ends with the first scan. No fast path. |
| `/subscope-profile` | Per-section deep dive for refining an existing profile. Not a full re-interview. |

**Pattern-specific scans** (each runs `fetch-score --mode <pattern>`)

| Skill | What it catches |
|---|---|
| `/subscope-pricing-rage` | Renewal-hike rage. Cooling queue skipped because these go cold fast. |
| `/subscope-churn` | "Looking to ditch X for..." posts. Active switching intent. |
| `/subscope-build-vs-buy` | In-house vs SaaS debates with real numbers. |
| `/subscope-rfp-bait` | "A vs B vs C" threads where multiple vendors are named. |
| `/subscope-stack-audit` | Someone lists 8+ tools and asks what to cut. Highest-intent format. |
| `/subscope-resurrect` | 6 to 18-month-old quality threads that still pull Google traffic. |
| `/subscope-rivals` | Today's mentions of any competitor in your `brand_anchor` list. |

**Utilities**

| Skill | What it does |
|---|---|
| `/subscope-pulse` | Weekly Obsidian digest of the week's surfaces. |
| `/subscope-op-vet <user>` | Vet a Reddit OP before replying. Returns an audience-fit read (which subs they post in) and a GO / HOLD / SKIP verdict. Karma and account age are best-effort: Reddit no longer serves them to unauthenticated clients, so the vet leans on audience fit and fails open when a signal is unavailable. |

Skill source files in [`skills/`](skills/).

</details>

---

## Setup

Run `/subscope-setup` to change any single layer (LLM provider, where surfaces land, integrations). It runs on day one with zero API keys.

Tuning knobs in `~/.config/subscope/weights.yml`:
- `daily_output.minimum` (default 5) and `tier2_per_sub_cap` (default 2) control how aggressively backfill kicks in when the gate pool is thin.
- `freshness_floor.max_age_hours` / `max_promoted` (defaults 24h / 3) control how many sub-24-hour posts get auto-promoted past the keyword gate.
- `authority_track.enabled` (default on) and `authority_track.cap` (default 4) control the Authority plays track.

Need more than 10 results per run? `--max-surfaces N` raises the cap. Want recurring scans? Wrap `subscope fetch-score` in cron or launchd. The SQLite cursor dedups across runs, so you never see the same thread twice.

Config lives at `~/.config/subscope/`. Every file is written with `chmod 600`.

---

## Privacy

- All data is local. SQLite at `~/.local/share/subscope/subscope.sqlite`, config at `~/.config/subscope/`. Both `0o600`.
- All credentials (LLM, DataForSEO, Firecrawl, Notion, Slack) are written atomically with `0o600` from the moment they appear on disk, no umask race.
- When bulk LLM grading, DataForSEO, or Firecrawl is enabled, the relevant data goes to your configured endpoint. A one-time stderr notice appears the first time per provider. Zero telemetry otherwise.

---

You find the thread. You write the reply. subscope handles the part that would take you an hour every morning.

MIT licensed, see [LICENSE](LICENSE). Issues and PRs welcome. The anti-positioning surface (no auto-posting, no drafting, no account rotation) is deliberate and load-bearing, so open an issue before proposing write-side Reddit features.

---

> From [NodeSparks](https://www.nodesparks.com) — [custom AI agents & automations](https://www.nodesparks.com/contact-us) for founders who would rather own the tool than rent it. subscope is our open-source buyer-signal scanner.
