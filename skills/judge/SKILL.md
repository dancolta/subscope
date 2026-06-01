---
name: subscope-judge
description: Interactive classifier for a single Reddit surface. Reads one post by surface ID (or pasted URL/title/body), runs the bulk-classifier prompt against it, and returns a verdict + reply angle. Uses your Claude Code subscription, NOT a separate API key — costs nothing extra beyond what you already pay. Triggers on "judge surface 3", "classify post #N", "/subscope-judge", "is post X a real lead", "should I reply to this", "judge this reddit post", or any request to evaluate a single surfaced post's quality before drafting a reply.
allowed-tools: Bash, Read
---

# /subscope-judge

Interactive single-surface classifier. Designed for the workflow where you scan today's `/subscope-run` output, hit one or two surfaces that feel borderline, and want a structured judgment before deciding whether to reply.

**Why this exists separately from bulk classification:** bulk LLM classification of every regex-passing post requires an Anthropic API key (~$0.50/day at 5K posts/day, cheap but not free). The judge skill uses your Claude Code subscription directly — no API key, no subprocess, no extra cost. It's the right tool for "I want a verdict on these 2 posts" not "grade every post in today's list."

## When to invoke

The user has just seen `/subscope-run` output and asks something like:

- `/subscope-judge 3` — surface number from today's list
- "is surface #5 actually a real lead?"
- "classify the HubSpot post"
- "judge this: https://reddit.com/comments/abc123/"

## Procedure

### Step 1 — Identify the target surface

Three paths the user might take:

**A. Surface number from today's list.** The most recent `inline_markdown` from `/subscope-run` is in your conversation context. Find the surface in the numbered list by index. Extract: post URL, title, body, subreddit.

**B. Reddit URL pasted.** Read the post via the engine's keyless RSS fetcher. Pass the URL through an environment variable (never interpolate it into the Python source, so a quote or special char in the URL cannot break the command):

```bash
cd "$CLAUDE_PLUGIN_ROOT" && PYTHONPATH=engine REDDIT_URL="$REDDIT_URL" python3 -c "
import os, sys, json
from subscope.lib import reddit
post = reddit.fetch_post(os.environ['REDDIT_URL'])
if not post:
    print('Could not reach that post. Check the URL, or paste the title and body directly and I will judge from that.', file=sys.stderr)
    sys.exit(1)
print(json.dumps({'subreddit': post['subreddit'], 'title': post['title'], 'body': (post['body'] or '')[:800]}))
"
```

`fetch_post` handles id extraction, validation, and the dual-host RSS failover internally. If it returns nothing (post removed, deleted, or Reddit unreachable), fall back to Path C: ask the user to paste the title and body.

**C. Free-text paste.** User pastes a title + body directly. No fetch needed — just structure it.

### Step 2 — Load the bulk-classifier prompt

You must use the SAME prompt the bulk classifier uses, so verdicts are consistent across modes. Read it:

```bash
cat "$CLAUDE_PLUGIN_ROOT/engine/subscope/prompts/classify.md"
```

This is the system prompt. Treat it as your operating instructions for this turn.

### Step 3 — Classify

Reason through the post per the prompt rules. The prompt expects strict JSON output. Produce a verdict matching this schema:

```json
{
  "intent": "pain_post" | "question" | "vendor_content" | "neutral",
  "buyer_stage": "unaware" | "considering" | "evaluating" | "ready" | "post_purchase",
  "sentiment": "positive" | "neutral" | "negative",
  "competitor_mentioned": "<brand>" | null,
  "fit_score": 0..10,
  "suggested_angle": "<short reply angle, max 120 chars, lowercase, anti-marketer>"
}
```

Apply the prompt's conservative rule: **if uncertain, lower fit_score.** Surface 5 is easier to skip than a wrong-6 to defend.

### Step 4 — Present to the user

Format the verdict for human reading, NOT raw JSON. Pattern:

```
**Surface N — r/<sub>**

> <one-line post title>

| | |
|---|---|
| Intent | <intent> |
| Buyer stage | <buyer_stage> |
| Sentiment | <sentiment> |
| Competitor mentioned | <brand or "none"> |
| Fit score | **N/10** |

**Reply angle:** <suggested_angle>

<one or two sentences of reasoning — what specifically signals the fit_score>
```

If `fit_score >= 7`, end with: `Go.`
If `fit_score 4-6`, end with: `Borderline — your call.`
If `fit_score <= 3`, end with: `Skip.`

## Anti-patterns

- Don't classify multiple surfaces in one invocation. That's bulk LLM territory — tell the user to set `ANTHROPIC_API_KEY` and re-run `/subscope-run` if they want every post graded.
- Don't draft a reply. The reply angle is a one-line hint; the human writes the actual comment.
- Don't promote any product (yours or anyone else's) in the angle. Anti-marketer voice is non-negotiable — read the prompt.
- Don't skip Step 2. The prompt is the source of truth. If it's missing or unreadable, fall back to a fast structural classification but tell the user the prompt couldn't be loaded.
