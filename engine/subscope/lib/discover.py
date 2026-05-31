"""Live subreddit discovery for `/subscope-onboard` step 5.

Replaces the static archetype-map seed list with subs harvested from real
Reddit threads matching the user's buyer-pain phrasing.

## Pipeline

1. `derive_queries(answers, scrape_markdown, competitors)` → 3-6 search queries
   built deterministically from T2/T3/T4 answers + Firecrawl scrape + DFS
   competitor list. No LLM in this path.
2. `search_dfs(query, conn)` → site:reddit.com SERP via DataForSEO. Harvests
   `r/<sub>/comments/...` URLs.
3. `search_reddit(query)` → Reddit's public /search.json. Always available,
   no creds, no cost.
4. `rank_subs(threads)` → per-sub score = (freq*2 + quality + recency*1.5) ×
   noise_mult. Top 8 with why-lines.
5. `discover_subs_for_profile(answers, homepage_url, conn)` → end-to-end
   entry point. Returns ranked subs + clarification signals for the skill.

## Fallback

- < 5 candidates after dedup + age filter + noise mult → `needs_clarification=True`
  with a vertical-clarifier prompt the skill renders as T4.5
- All providers down or hard timeout → returns whatever Tier B got, with a
  `discovery_unreachable=True` flag the skill renders as a warning on T5

The archetype map is NOT replaced for downstream scoring (keywords,
brand_anchor, example_pains still flow from `profile_synth.fallback_from_archetype`).
Only the T5 candidate-sub surface uses discovery output.
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import enrich, net, reddit, score, store


# ─── Tunables (mirrored in config/weights.yml `discovery:` block) ──────

NOISE_DOWNRANK_FACTOR = 0.5
MIN_SUBS_THRESHOLD = 3                # v3: lower threshold, quality over quantity
MAX_SUBS_RETURNED = 8                 # final cap after Phase B
PHASE_A_CANDIDATE_CAP = 12            # Phase A returns 12, Phase B trims to 8
MAX_QUERIES = 6
DISCOVERY_HARD_TIMEOUT_S = 30.0       # raised from 20 to absorb Phase B
PHASE_B_TIMEOUT_S = 22.0              # hard cap on Phase B validation (search-within-sub is slightly heavier than scan)
PHASE_B_FRESH_WINDOW_HOURS = 48       # daily-scan freshness window
# Onboarding sub-DISCOVERY uses a wider window than the daily scan. Rationale
# (backed by v3.1 QA on 10 niche-B2B businesses): at a strict 48h gate, half
# the businesses had ZERO subs with a buyer-intent thread on the run day,
# because niche-B2B switching conversations are weekly/monthly, not daily.
# Discovery is choosing which subs to WATCH long-term; a sub with a real buyer
# thread 4 days ago is a great sub to watch. The daily scan still uses 48h to
# surface today's hot threads. Absolute timestamps are shown either way so the
# user sees exactly how fresh each evidence thread is (no "ancient thread"
# surprise, which was the original complaint).
DISCOVERY_FRESH_WINDOW_HOURS = 168    # 7 days, for onboarding sub-discovery
# Engine surface floor. The engine is the RECALL stage: it surfaces every sub
# with a fresh thread that passed the deterministic buyer-intent gate, so a
# genuinely on-topic sub like r/WhichCRM (confidence in the 30s) is NOT dropped
# just for being single-thread. Final PRECISION comes from the skill-layer
# relevance review (the orchestrating Claude reads each evidence thread and
# drops semantic false positives the regex gate cannot catch, e.g. a
# "Software Engineering vs Dentistry" career question). The confidence band is
# shown to the user as a quality signal, not used as a hard cutoff.
CONFIDENCE_FLOOR = 20                 # drop only the very weakest gate-passers

# Per-thread relevance (v3.1): a fresh thread is software-buyer-intent only if
# an intent token CO-OCCURS with a product/purchase noun (medium path) OR a
# competitor brand is named (strong path). The product-noun whitelist is
# deliberately DISJOINT from user vocabulary: a vocab token alone (e.g. the
# word "podcast" for a podcast tool) must never qualify a thread, or
# "looking for interviewees for my podcast" leaks through. User vocab keeps
# its role only in sub-NAME ranking (rank_subs), not the per-thread gate.
_PRODUCT_NOUNS = (
    # Strong software-product nouns (rarely appear in non-software chatter)
    "software", "saas", "crm", "erp", "ehr", "emr", "pms", "api",
    "app", "apps", "platform", "dashboard", "integration", "integrations",
    "tool", "tools", "vendor", "vendors", "suite",
    "subscription", "subscriptions", "automation", "automations", "nocode",
    # NOTE: deliberately EXCLUDED generic/domain-overlap nouns that leak on
    # non-software threads: billing, invoice, price, pricing, plan, seat,
    # service, system, license, workflow, stack, solution. ("Medical billing
    # vs Coding" fired the noun path via 'billing' in v3.1 QA.) Competitor-brand
    # mentions still catch buyers who name a tool without a generic noun.
)
# Buyer-intent tokens (multi-word phrases matched first). Mirrors the prose set.
_INTENT_TOKENS = (
    "instead of", "moving from", "moving off", "looking for", "better than",
    "any good", "anyone use", "anyone tried", "what do you use",
    "experience with", "thoughts on", "alternatives", "alternative",
    "switching", "switch", "replacing", "replacement", "replace", "versus",
    "vs", "compare", "comparison", "recommendations", "recommendation",
    "recommend", "suggestions", "cheaper", "migrate", "ditching", "ditched",
    "ditch", "best",
)
# Narrow negation guard: these within 3 tokens BEFORE an intent token suppress it.
_NEGATION_TOKENS = ("not", "no", "dont", "isnt", "stop", "avoid", "without")
# Co-occurrence window (tokens) between an intent token and a product noun.
_INTENT_NOUN_WINDOW = 10

# Per-sub quality is capped at 3.0 (was 5.0). Quality is a tiebreaker, not a
# dominator. Cross-query frequency is the real signal that a sub is a place
# people discuss the buyer's problem, not a single viral thread.
QUALITY_CEILING = 3.0

# Hard gate: a sub MUST have at least this many threads matched across all
# queries to make the output. Single-thread matches are almost always
# coincidental lexical overlap.
MIN_THREAD_COUNT = 2

# v3: freshness cutoff is now Phase B's job, not Phase A's. Phase A intentionally
# accepts older threads (they tell us WHERE the conversation lives historically).
# Phase B validates that the sub has ACTIVE buyer-intent activity in the last 48h.
# The 730-day Phase A cutoff is kept loose so Phase A finds candidates; Phase B
# enforces the real freshness gate.
FRESHNESS_CUTOFF_DAYS = 730

# Vocabulary-overlap bonus: when the sub name shares a token with the user's
# own answers (T2/T3/T4 lowercase, stopwords stripped, ≥4 chars), the sub
# gets a score boost. This is fully data-driven: an accounting SaaS seller
# gets r/Accounting bonused but NOT r/ecommerce, while a Shopify app seller
# gets r/ecommerce bonused but NOT r/Accounting. Replaces the brittle static
# operator-token list (which had no awareness of who the buyer actually is).
USER_VOCAB_BONUS = 2.5

# ─── Subreddit-directory discovery (recall) ────────────────────────────
# Thread-host extraction (search.rss over POSTS) finds subs where a phrase like
# "asana alternative" was posted, which skews to indie / maker / self-host subs,
# not the category community the ICP actually lives in. Searching Reddit's
# SUBREDDIT directory for the offer's category nouns plus competitor names
# surfaces the real communities (r/projectmanagement, r/agency, r/consulting).
# These constants score a directory hit relative to the thread-frequency signal.
DIRECTORY_BASE = 8.0                # base score for a Reddit-recognized community match
DIRECTORY_QUERY_WEIGHT = 3.0        # each ADDITIONAL category query that surfaced the sub
COMPETITOR_COMMUNITY_BONUS = 5.0    # the sub IS a competitor's own community (r/<brand>)
MAX_SUB_QUERIES = 8                 # cap on subreddit-directory queries per discovery
# Suffixes a competitor's own community sub may carry after the brand token, so
# r/ClickUpApp counts as ClickUp's community but r/AsAnAlly (asana + "lly") does
# not. Empty string allows the exact brand sub (r/Asana).
_COMMUNITY_SUFFIXES = {"", "app", "hq", "official", "community", "users", "io", "crm", "pm"}
# Same, minus the empty string, for de-dotted brand bases (so r/mondaydotcom
# matches Monday.com via base "monday" + "dotcom", but the bare r/monday weekday
# sub does NOT match via an empty suffix).
_NONEMPTY_COMMUNITY_SUFFIXES = {"app", "hq", "official", "community", "users", "io", "crm", "pm", "dotcom"}

# Generic vocabulary stopwords stripped from token overlap.
# Conservative list: ONLY true linguistic stopwords + the most generic
# nouns. Domain-meaningful words like "saas", "alternatives", "expensive"
# stay in vocabulary so that when a user explicitly writes them, subs with
# those tokens in their names (r/microsaas, r/B2BSaaS) get vocab match.
_VOCAB_STOPWORDS = {
    # function words
    "the", "and", "for", "with", "that", "this", "have", "from", "your",
    "you", "our", "their", "they", "are", "was", "were", "been", "being",
    "any", "all", "some", "more", "most", "much", "many", "few", "such",
    "very", "just", "only", "also", "into", "onto", "out", "off",
    "good", "bad", "new", "old", "big", "small", "high", "low",
    # extremely generic nouns/verbs
    "want", "wants", "need", "needs", "like", "likes", "use", "uses",
    "make", "makes", "made", "get", "gets", "got",
    "thing", "things", "stuff", "people", "team", "teams",
    "company", "companies", "service", "services",
}

# Subreddit names that overlap the user's likely intent but are noise-heavy.
# Downrank 50%, do not drop. Three sub-groups:
#  (a) generic founder / SaaS noise, original list per market researcher
#  (b) finance / investing subs that match "SaaS subscriptions expensive" as
#      stock-analysis chatter, not buyer-intent (caught in live smoke)
#  (c) general-venting / lifestyle subs that match price-rage queries because
#      Reddit's search is lexical, not semantic (also caught in live smoke)
NOISE_DOWNRANK_SUBS = {
    # (a) generic founder / SaaS noise
    "entrepreneur", "saas", "smallbusiness", "startups",
    "coldemail", "emailmarketing", "marketing", "digital_marketing",
    "business", "entrepreneurridealong",
    # (b) finance / investing, false positives on SaaS-pricing queries
    "wallstreetbets", "valueinvesting", "stocks", "investing",
    "personalfinance", "frugal", "fire",
    # (c) general venting / lifestyle, false positives on price-rage
    "mildlyinfuriating", "middleclasshq", "antiwork", "layoffs",
    "politics", "news", "worldnews", "askreddit", "showerthoughts",
    "rant", "venting", "complaints",
    # (d) drama / relationship / meta repost subs that lexically match
    # any phrase containing "expensive", "switching", "alternative" in
    # personal-life contexts (caught in accounting-persona smoke)
    "aitah", "amitheasshole", "amioverreacting", "relationships",
    "relationship_advice", "bestofredditorupdates", "tifu",
    "twoxchromosomes", "askmen", "askwomen", "futurology",
    # (e) indie-maker / build-in-public / promo subs. "X alternative" and
    # "replacing X" threads get POSTED here by makers, so thread-host extraction
    # over-surfaces them, but the ICP buying the tool is not here. (caught in the
    # Teamwork discovery QA: r/SideProject, r/BuyFromEU, r/buildinpublic topped
    # the list ahead of r/projectmanagement.)
    "sideproject", "sideprojects", "buildinpublic", "indiehackers", "indiebiz",
    "indiedev", "entrepreneurs", "sidehustle", "juststart", "growmybusiness",
    "buyfromeu", "buyitforlife", "alphaandbetausers", "roastmystartup",
}

# Buyer-intent signal: a thread is dropped from discovery if its title contains
# none of these tokens. The bar is "the OP is shopping, comparing, or switching"
# not "the OP is venting about prices." Tokens are matched as whole words
# (case-insensitive, word-boundary).
BUYER_INTENT_TOKENS = (
    "alternative", "alternatives", "switching", "switch",
    "replace", "replacing", "replacement", "instead of",
    "vs", "versus", "compare", "comparison",
    "looking for", "recommend", "recommendation", "suggestions",
    "cheaper", "migrate", "moving from", "moving off",
    "ditching", "ditch", "ditched", "best", "better than",
    "any good", "anyone use", "anyone tried", "what do you use",
    "experience with", "thoughts on",
)

_BUYER_INTENT_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in BUYER_INTENT_TOKENS) + r")\b",
    re.IGNORECASE,
)


def _build_user_vocabulary(answers: dict[str, str]) -> set[str]:
    """Extract the user's domain vocabulary from T2/T3/T4.

    Returns lowercase tokens ≥4 chars, stripped of generic stopwords. This
    becomes the "is this sub topically related to what the user sells"
    signal for the ranking bonus.
    """
    blob = " ".join([
        answers.get("what_offering", "") or "",
        answers.get("who_to_reach", "") or "",
        answers.get("pain_quote", "") or "",
    ]).lower()
    tokens = re.findall(r"[a-z][a-z\-]{3,}", blob)
    return {t for t in tokens if t not in _VOCAB_STOPWORDS}


def _sub_matches_user_vocab(sub_key: str, vocab: set[str]) -> bool:
    """True if any vocabulary token appears as a substring of the sub name.

    Substring match (not word-boundary) because sub names compress tokens:
    'aiautomations' contains 'automation', 'salesops' contains 'sales'.
    """
    if not sub_key or not vocab:
        return False
    s = sub_key.lower()
    return any(tok in s for tok in vocab)


def _has_buyer_intent(title: str, body: str = "") -> bool:
    """True if the thread title or body looks like the OP is shopping /
    comparing / switching, not just venting.

    Body is scanned in addition to title because fresh-post discovery (Phase B)
    often surfaces threads where the intent token is in the body, not the title
    (e.g. title 'Anyone here?' + body 'looking for alternative to X'). Empty
    title AND body is treated as no-intent.
    """
    if not title and not body:
        return False
    blob = title or ""
    if body:
        blob = blob + " " + body[:500]
    return bool(_BUYER_INTENT_RE.search(blob))

# Reddit sub-name rule: alphanumerics + underscore, 2-21 chars.
_SUBNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")

# Subreddit-search Atom <link href>: https://www.reddit.com/r/<name>/
_SR_LINK_RE = re.compile(r"/r/([A-Za-z0-9_]{2,21})/?", re.IGNORECASE)

# DFS SERP harvest: r/<sub>/comments/<id>/...
_DFS_SUB_RE = re.compile(r"reddit\.com/r/([A-Za-z0-9_]{2,21})/comments/", re.IGNORECASE)

# T2 noun-phrase extraction: noun after "for" or before "tool"/"platform"/"software".
_OFFERING_HEAD_RE = re.compile(
    r"\bfor\s+([A-Za-z][\w\s\-]{2,40})|"
    r"([A-Za-z][\w\s\-]{2,40})\s+(?:tool|platform|software|app|service)\b",
    re.IGNORECASE,
)

# Homepage pain extraction (rule 6 in spec).
_HOMEPAGE_PAIN_RE = re.compile(
    r"(?:stuck on|tired of|frustrated with|hate|switching from|replacing)\s+([^.\n]{8,60})",
    re.IGNORECASE,
)

# T4 price-rage trigger.
_PRICE_RAGE_RE = re.compile(r"\b(expensive|price|raised|increase|cost|bill|charging|hike)\b", re.IGNORECASE)

# Stopwords for buyer-noun rule.
_BUYER_STOPWORDS = {
    "the", "a", "an", "for", "of", "in", "at", "to", "and", "or",
    "with", "people", "person", "user", "users", "team", "teams",
    "company", "companies", "business", "businesses",
}

# Role / seniority words stripped when mining single-token audience queries from
# who_to_reach (so "agencies" and "consulting" become directory queries but
# "managers" / "leads" / "planners" do not).
_ROLE_STOPWORDS = {
    "manager", "managers", "lead", "leads", "leader", "leaders", "planner",
    "planners", "director", "directors", "owner", "owners", "head", "heads",
    "team", "teams", "firm", "firms", "company", "companies", "people",
    "staff", "professional", "professionals", "rep", "reps", "specialist",
    "specialists", "executive", "executives", "founder", "founders",
}

# Prepositions/connectors that can leak into the greedy industry-noun capture
# tail (e.g. "... at agencies serving healthcare") and must not become queries.
_AUDIENCE_PREP_SKIP = {"serving", "across", "within", "through"}

# Leading prefixes to strip from the verbatim T4 pain.
_T4_LEADING_PREFIXES = ("i'm ", "i am ", "we're ", "we are ", "my ", "our ", "the ")

_QUERY_MAX_CHARS = 80


def _log(msg: str) -> None:
    sys.stderr.write(f"[discover] {msg}\n")
    sys.stderr.flush()


# ─── Per-thread software-buyer-intent classifier (v3.1) ────────────────


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens. Used for proximity math in the intent gate."""
    return re.findall(r"[a-z0-9][a-z0-9\-]*", (text or "").lower())


def _intent_hits(tokens: list[str]) -> list[tuple[int, str]]:
    """List of (start_index, matched_phrase) for buyer-intent tokens / phrases,
    excluding hits negated by a preceding negation token within 3 tokens.

    Returns the verbatim matched phrase (single token or multi-word) so the
    reason string can quote exactly what fired (truthfulness contract)."""
    hits: list[tuple[int, str]] = []
    single = {t for t in _INTENT_TOKENS if " " not in t}
    for i, tok in enumerate(tokens):
        if tok in single:
            hits.append((i, tok))
    multi = [t for t in _INTENT_TOKENS if " " in t]
    for phrase in multi:
        ptoks = phrase.split()
        plen = len(ptoks)
        for i in range(len(tokens) - plen + 1):
            if tokens[i:i + plen] == ptoks:
                hits.append((i, phrase))
    # Negation guard: drop hits with a negation token within 3 before the start
    kept = []
    for idx, phrase in hits:
        window_before = tokens[max(0, idx - 3):idx]
        if any(n in window_before for n in _NEGATION_TOKENS):
            continue
        kept.append((idx, phrase))
    # Stable sort by index
    return sorted(set(kept), key=lambda x: x[0])


def _noun_positions(tokens: list[str]) -> list[int]:
    return [i for i, t in enumerate(tokens) if t in _PRODUCT_NOUNS]


# Common English words that appear as the FIRST word of multi-word brand names
# ("When I Work", "Open Dental", "Jane App"). Never promote these to standalone
# brand-match tokens, or every "when"/"open"/"make" in a post triggers a hit.
_COMMON_BRAND_FIRST_WORDS = {
    "when", "work", "open", "jane", "make", "time", "best", "your", "cloud",
    "smart", "field", "house", "quick", "money", "world", "store", "sales",
    "team", "base", "home", "active", "monday", "better", "simple", "easy",
    "good", "free", "pro", "the", "my", "go", "get",
}


def _build_competitor_tokens(competitors: list[str] | None) -> set[str]:
    """Build the set of competitor match tokens.

    - Full brand name always added (matched word-boundary for single words,
      substring for dotted/multi-word names in _matched_competitor).
    - First word of a MULTI-word brand added ONLY if it is distinctive:
      length >= 5 AND not a common English word. So "Drake Software" -> "drake"
      (kept), but "When I Work" -> "when" is dropped (the full phrase
      "when i work" still matches). This kills the v3.1 false positive where
      "when" matched the competitor path on an unrelated r/managers thread.
    """
    tokens: set[str] = set()
    for c in (competitors or []):
        c_lower = (c or "").strip().lower()
        if not c_lower:
            continue
        tokens.add(c_lower)
        if " " in c_lower:
            first = c_lower.split()[0]
            if len(first) >= 5 and first not in _COMMON_BRAND_FIRST_WORDS:
                tokens.add(first)
    return tokens


@dataclass
class BuyerIntentMatch:
    """Result of the per-thread software-buyer-intent classifier.

    Tuple-unpackable as (passed, path, weight) for backward compatibility with
    existing call sites, while carrying the matched-token detail needed to
    build a 100%-truthful reason string.
    """
    passed: bool
    path: str | None          # "competitor" | "noun" | None
    weight: float             # 1.0 | 0.6 | 0.0
    competitor: str | None = None   # exact brand token matched, e.g. "buzzsprout"
    noun: str | None = None         # exact product noun matched, e.g. "tool"
    intent_token: str | None = None  # exact intent token/phrase, e.g. "vs"
    marker: str | None = None        # "pain" | "question" (competitor path w/o intent)

    def __iter__(self):
        # Allow `passed, path, weight = software_buyer_intent(...)`
        yield self.passed
        yield self.path
        yield self.weight


def _matched_competitor(title: str, body: str, comp_tokens: set[str]) -> str | None:
    """Return the exact competitor token that matched, or None."""
    blob = (title + " " + body[:800]).lower()
    for c in comp_tokens:
        if not c:
            continue
        if " " in c or "." in c:
            if c in blob:
                return c
        else:
            if re.search(rf"\b{re.escape(c)}\b", blob):
                return c
    return None


def software_buyer_intent(
    title: str,
    body: str,
    comp_tokens: set[str],
) -> "BuyerIntentMatch":
    """Classify whether a fresh Reddit thread is a software buyer signal.

    Returns a BuyerIntentMatch (tuple-unpacks to passed, path, weight):
      - competitor (weight 1.0): names a competitor brand AND (has an intent
        token OR a pain/question marker). Strong signal.
      - noun (weight 0.6): an intent token co-occurs with a product noun within
        _INTENT_NOUN_WINDOW tokens, in title or body. Medium signal.
      - neither (False, weight 0.0): the "looking for interviewees for my
        podcast" class lands here.

    The product-noun whitelist is disjoint from user vocab on purpose: a topic
    word alone never qualifies a thread. The matched tokens are captured so the
    reason string can quote exactly what fired.
    """
    title = title or ""
    body = body or ""

    # Strong path: competitor brand + (intent OR pain/question)
    brand = _matched_competitor(title, body, comp_tokens) if comp_tokens else None
    if brand:
        t_hits = _intent_hits(_tokenize(title))
        b_hits = _intent_hits(_tokenize(body[:800]))
        intent_phrase = (t_hits[0][1] if t_hits else (b_hits[0][1] if b_hits else None))
        if intent_phrase:
            return BuyerIntentMatch(True, "competitor", 1.0,
                                    competitor=brand, intent_token=intent_phrase)
        if score.has_pain_markers(title, body):
            return BuyerIntentMatch(True, "competitor", 1.0,
                                    competitor=brand, marker="pain")
        if score.has_question_intent(title, body):
            return BuyerIntentMatch(True, "competitor", 1.0,
                                    competitor=brand, marker="question")
        # bare brand + neutral (changelog/news) does NOT pass
        return BuyerIntentMatch(False, None, 0.0, competitor=brand)

    # Medium path: intent token co-occurs with a product noun within window.
    # Check title and body as SEPARATE spans (no cross-field pairing).
    for field in (title, body[:800]):
        toks = _tokenize(field)
        i_hits = _intent_hits(toks)
        if not i_hits:
            continue
        noun_idx = _noun_positions(toks)
        if not noun_idx:
            continue
        for ii, phrase in i_hits:
            for ni in noun_idx:
                if abs(ii - ni) <= _INTENT_NOUN_WINDOW:
                    return BuyerIntentMatch(True, "noun", 0.6,
                                            noun=toks[ni], intent_token=phrase)

    return BuyerIntentMatch(False, None, 0.0)


def build_reason(match: "BuyerIntentMatch", age_h: float | None) -> str | None:
    """Assemble a truthful, plain-English reason string from a passing match.

    Only names signals that actually fired (every quoted substring was captured
    from the thread by the matcher). Returns None for a non-passing match.
    No em dashes (user-facing).
    """
    if not match.passed:
        return None

    # Freshness gloss
    if age_h is None:
        fresh = "Recent buyer post."
    elif age_h < 1:
        fresh = "Buyer post under 1h ago."
    elif age_h < 48:
        fresh = f"Buyer post {round(age_h)}h ago."
    else:
        fresh = f"Buyer post {round(age_h / 24)}d ago."

    if match.path == "competitor":
        brand = (match.competitor or "a competitor").strip()
        brand_disp = brand if brand else "a competitor"
        if match.intent_token:
            clause = (f'Someone weighing {brand_disp} against alternatives '
                      f'("{match.intent_token}").')
        elif match.marker == "pain":
            clause = (f"Someone naming {brand_disp} with a complaint about "
                      f"their current tool.")
        elif match.marker == "question":
            clause = f"Someone discussing {brand_disp} with a buying question."
        else:
            clause = f"Someone discussing {brand_disp}."
        return f"{fresh} {clause}"

    if match.path == "noun":
        noun = match.noun or "a tool"
        tok = match.intent_token or "shopping language"
        return (f'{fresh} Someone asking about a "{noun}" with buying intent '
                f'("{tok}").')

    return fresh


# ─── 1. Query derivation ────────────────────────────────────────────────


def _normalize_query(s: str) -> str:
    """Lowercase, strip, collapse whitespace, trim to 80 chars at word boundary.

    Only strips matched outer quote pairs (so a fully quoted competitor query
    `"drake software"` becomes `drake software`) but preserves internal quotes
    needed for Reddit's exact-phrase search (`replacing "drake software"`
    survives intact).
    """
    s = re.sub(r"\s+", " ", (s or "").strip().lower())
    # Only strip if BOTH ends are the same quote char (matched pair)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        s = s[1:-1].strip()
    for prefix in _T4_LEADING_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if len(s) <= _QUERY_MAX_CHARS:
        return s
    cut = s.rfind(" ", 0, _QUERY_MAX_CHARS)
    return (s[:cut] if cut > 0 else s[:_QUERY_MAX_CHARS]).strip()


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _add_query(out: list[str], token_sets: list[set[str]], candidate: str) -> bool:
    """Append `candidate` to `out` if (a) non-empty and (b) Jaccard < 0.7 vs
    all previously added queries. Returns True if added."""
    c = _normalize_query(candidate)
    if not c or len(c) < 6:
        return False
    cs = _tokens(c)
    for existing in token_sets:
        if _jaccard(cs, existing) >= 0.7:
            return False
    out.append(c)
    token_sets.append(cs)
    return True


_BUYER_NOUN_PRIORITY = (
    "founder", "founders", "operator", "operators", "owner", "owners",
    "ceo", "cto", "coo", "cfo", "manager", "managers", "director",
    "principal", "head", "lead", "leader", "leaders", "freelancer",
    "agency", "agencies", "consultant", "consultants",
)


def _extract_buyer_noun(t3: str) -> str | None:
    """Head noun from T3. Prefers known buyer-role tokens when present
    anywhere in the answer (founder, operator, owner, ceo, etc.), falls back
    to the first noun-before-preposition pattern, then the last non-stopword
    alphabetic token."""
    if not t3:
        return None
    s = t3.lower().strip()
    # Priority pass: known buyer-role tokens anywhere in T3
    for role in _BUYER_NOUN_PRIORITY:
        if re.search(rf"\b{role}\b", s):
            # Drop trailing 's' for singular form (founders -> founder still works)
            return role
    # Fallback: "founders at a SaaS startup" → "founders"
    m = re.match(r"^([\w\-]+)\s+(?:at|of|in)\s+", s)
    if m:
        cand = m.group(1)
    else:
        # Take last alphabetic token > 2 chars not in stopwords
        words = re.findall(r"[a-z][a-z\-]+", s)
        cand = None
        for w in reversed(words):
            if len(w) > 2 and w not in _BUYER_STOPWORDS:
                cand = w
                break
    if not cand or cand in _BUYER_STOPWORDS:
        return None
    return cand


def _extract_offering_pain(t2: str) -> str | None:
    """T2 offering pain phrase per rule 5."""
    if not t2:
        return None
    m = _OFFERING_HEAD_RE.search(t2)
    if not m:
        return None
    phrase = (m.group(1) or m.group(2) or "").strip()
    return phrase if phrase else None


def _extract_homepage_pain(markdown: str) -> str | None:
    """First match of homepage pain regex (rule 6)."""
    if not markdown:
        return None
    m = _HOMEPAGE_PAIN_RE.search(markdown)
    if not m:
        return None
    return m.group(1).strip(" .,;:")


def derive_queries(
    answers: dict[str, str],
    scrape_markdown: str | None = None,
    competitors: list[str] | None = None,
    vertical: str | None = None,
) -> list[str]:
    """Build 3-6 search queries from interview answers + scrape + competitor list.

    Rules fire in fixed order; each adds to `out` only if Jaccard < 0.7 vs
    already-queued. Maximum MAX_QUERIES queries. See module docstring for the
    derivation spec.
    """
    t2 = (answers.get("what_offering") or "").strip()
    t3 = (answers.get("who_to_reach") or "").strip()
    t4 = (answers.get("pain_quote") or "").strip()
    competitors = competitors or []

    out: list[str] = []
    token_sets: list[set[str]] = []

    # Rule 1: T4 pain, truncated at first major punctuation. The full T4 can
    # be 200+ chars (Claude synthesizes a paragraph after the URL fetch); a
    # short cleaned phrase searches better than a comma-spliced paragraph.
    if len(t4) >= 8:
        t4_first = re.split(r"[.,;:!?]|\s+\(", t4, maxsplit=1)[0].strip()
        if len(t4_first) >= 8:
            _add_query(out, token_sets, t4_first)
        elif t4:
            _add_query(out, token_sets, t4)

    # Rule 2: pain + buyer-noun
    if t4 and len(out) < MAX_QUERIES:
        buyer = _extract_buyer_noun(t3)
        if buyer:
            _add_query(out, token_sets, f"{t4} {buyer}")

    # Rule 2.5: vertical (only fires on Tier-A retry after clarifier)
    if vertical and len(out) < MAX_QUERIES:
        v_norm = vertical.strip().lower()
        if v_norm and v_norm != "general":
            buyer = _extract_buyer_noun(t3) or ""
            _add_query(out, token_sets, f"{v_norm} {buyer} {t4}".strip())

    # Rule 3: replacing-competitor (up to 2 competitors).
    # Multi-word names get wrapped in quotes so Reddit treats them as exact
    # phrases. Without quotes, "Drake Software" matches gaming subs that
    # mention "Drake" (the rapper). With quotes, only threads with the literal
    # two-word phrase match. Single-word names (n8n, Salesforce, Zapier)
    # skip the wrap since they're already unambiguous.
    used_competitors = 0
    for c in competitors:
        if used_competitors >= 2 or len(out) >= MAX_QUERIES:
            break
        c = (c or "").strip().lower()
        if not c or "." in c[:3]:  # skip TLD-only or empty
            continue
        # Drop TLD for cleaner phrasing: "instantly.ai" -> "instantly"
        c_clean = c.split(".")[0] if "." in c else c
        # Quote-wrap if multi-word for Reddit's exact-phrase search
        c_phrase = f'"{c_clean}"' if " " in c_clean else c_clean
        if _add_query(out, token_sets, f"replacing {c_phrase}"):
            used_competitors += 1
        if used_competitors < 2 and len(out) < MAX_QUERIES:
            _add_query(out, token_sets, f"{c_phrase} alternative")

    # Rule 4: price-rage variant (conditional on T4 matching the trigger)
    if t4 and _PRICE_RAGE_RE.search(t4) and len(out) < MAX_QUERIES:
        _add_query(out, token_sets, "saas price increase alternatives")

    # Rule 5: offering pain (T2 noun + "too expensive" or "replacement")
    offering = _extract_offering_pain(t2)
    if offering and len(out) < MAX_QUERIES:
        # Pick the suffix that adds new tokens vs what's already queued
        for suffix in ("too expensive", "replacement", "alternatives"):
            if _add_query(out, token_sets, f"{offering} {suffix}"):
                break

    # Rule 6: homepage-derived pain
    if scrape_markdown and len(out) < MAX_QUERIES:
        homepage_pain = _extract_homepage_pain(scrape_markdown)
        if homepage_pain:
            _add_query(out, token_sets, homepage_pain)

    # Rule 7: T4 noun-phrase extraction. Pulls 2-3 word phrases that aren't
    # stopwords (e.g. "GTM tool stack", "per-seat pricing", "cold email infra")
    # and appends " alternatives" so Reddit's index matches buying intent.
    if t4 and len(out) < MAX_QUERIES:
        for phrase in _extract_noun_phrases(t4):
            if len(out) >= MAX_QUERIES:
                break
            _add_query(out, token_sets, f"{phrase} alternatives")

    return out


def _audience_industry_nouns(text: str) -> list[str]:
    """Pull industry / vertical nouns from who-you-reach.

    The industry context usually follows a preposition: "PMs AT agencies",
    "ops leads IN consulting firms". Those nouns (agencies, consulting) are the
    community-category words that map to real subreddits, unlike the role words
    that come before them (managers, leads). Returns deduped lowercase nouns in
    order of appearance.
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\b(?:at|in|serving|for|across|within)\s+([a-z][\w\s,&\-]{2,70})",
                         (text or "").lower()):
        for tok in re.findall(r"[a-z][a-z\-]{4,}", m.group(1)):
            if (tok in _ROLE_STOPWORDS or tok in _VOCAB_STOPWORDS
                    or tok in _BUYER_STOPWORDS or tok in _AUDIENCE_PREP_SKIP
                    or tok in seen):
                continue
            seen.add(tok)
            out.append(tok)
    return out


def derive_sub_queries(
    answers: dict[str, str],
    competitors: list[str] | None = None,
    vertical: str | None = None,
    max_queries: int = MAX_SUB_QUERIES,
) -> list[str]:
    """Build category/audience/competitor queries for the SUBREDDIT-directory search.

    Unlike derive_queries (which builds "X alternative" THREAD searches), this asks
    Reddit which COMMUNITIES match the offer's category, the buyer's industry, and
    each competitor's own community. That is what surfaces r/projectmanagement,
    r/agency, r/consulting and r/<competitor> instead of the indie / maker subs
    where "X alternative" threads happen to get posted. Priority order guarantees
    the highest-recall queries (competitors + industry nouns + the lead category
    phrase) make the cap before lower-signal phrases fill the rest.
    """
    competitors = competitors or []
    t2 = answers.get("what_offering") or ""
    t3 = answers.get("who_to_reach") or ""
    out: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = (q or "").strip().lower()
        if not q or len(q) < 3 or q in seen or len(out) >= max_queries:
            return
        seen.add(q)
        out.append(q)

    comp_q = [(c or "").strip().lower().split(".")[0] for c in competitors[:3]]
    comp_q = [c for c in comp_q if c]
    industry = _audience_industry_nouns(t3)
    off_phrases = _extract_noun_phrases(t2)
    aud_phrases = _extract_noun_phrases(t3)

    for q in comp_q[:2]:
        add(q)
    for q in industry[:2]:
        add(q)
    for q in off_phrases[:1]:
        add(q)
    if vertical and vertical.strip().lower() != "general":
        add(vertical)
    for q in off_phrases[1:2]:
        add(q)
    for q in aud_phrases[:1]:
        add(q)
    for q in industry[2:3]:
        add(q)
    for q in comp_q[2:3]:
        add(q)
    return out[:max_queries]


def _extract_noun_phrases(text: str) -> list[str]:
    """Pull 2-3 word noun phrases from T4 / T2 free text.

    Heuristic: split on punctuation, then on each chunk pull groups of
    consecutive alphabetic tokens (each > 2 chars, none in stopwords) of
    length 2-3. Returns deduped phrases in order of appearance.

    Example input: "SaaS price hikes, per-seat tax compounding as team grows,
    expensive GTM tool stacks (outreach, scrapers, enrichment)"
    Example output: ["saas price hikes", "per-seat tax compounding",
    "gtm tool stacks"]
    """
    if not text:
        return []
    chunks = re.split(r"[.,;:!?()\[\]]|\s+(?:and|or|with|of|the|for|as|in|at)\s+",
                      text.lower())
    out: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        tokens = re.findall(r"[a-z][a-z\-]{2,}", chunk)
        # Walk windows of 2-3 consecutive non-stopword tokens
        for size in (3, 2):
            for i in range(len(tokens) - size + 1):
                window = tokens[i:i + size]
                if all(w not in _BUYER_STOPWORDS and len(w) > 2 for w in window):
                    phrase = " ".join(window)
                    if phrase not in seen and len(phrase) >= 8:
                        seen.add(phrase)
                        out.append(phrase)
        if len(out) >= 4:
            break
    return out[:4]


# ─── 2. Search execution ───────────────────────────────────────────────


def search_reddit(query: str, sleep_between: float = 0.5) -> list[dict[str, Any]]:
    """Reddit native search via RSS (search.rss). Keyless, no cost, throttled.

    Reddit's edge 403-blocks unauthenticated /search.json, so discovery reads the
    Atom search feed instead (same data minus engagement counts). Returns a list
    of normalized threads: {sub, title, score, num_comments, created_utc,
    source_query, source: "reddit_native", permalink}. RSS carries no score /
    num_comments, so those are 0 (recall ranks on sub + freshness + buyer intent,
    not engagement). Shares the global per-IP rate-limit throttle via fetch_feed.
    """
    if not query:
        return []
    encoded = urllib.parse.quote(query)
    # old.reddit.com RSS first (historically lighter limiting), www fallback.
    primary = (
        f"https://old.reddit.com/search.rss?q={encoded}"
        f"&restrict_sr=off&sort=relevance&limit=25"
    )
    posts = reddit.fetch_feed(primary)
    if posts is None:
        fallback = (
            f"https://www.reddit.com/search.rss?q={encoded}"
            f"&restrict_sr=off&sort=relevance&limit=25"
        )
        posts = reddit.fetch_feed(fallback)
    if not posts:
        return []
    threads: list[dict[str, Any]] = []
    for p in posts:
        sub = (p.get("subreddit") or "").strip()
        if not sub or not _SUBNAME_RE.match(sub):
            continue
        title = p.get("title", "")
        # Intent gate (hybrid): noise-listed subs MUST have a buyer-intent
        # token in the title (otherwise wallstreetbets + venting subs leak in
        # via lexical match). Subs NOT in the denylist get a free pass; the
        # fact that a clean operator sub is surfacing for the user's pain
        # phrasing is itself the signal, even if the specific title doesn't
        # contain "alternative" or "switching".
        sub_key = sub.lower()
        if sub_key in NOISE_DOWNRANK_SUBS and not _has_buyer_intent(title):
            continue
        threads.append({
            "sub": sub,
            "title": title,
            "score": int(p.get("score") or 0),
            "num_comments": int(p.get("num_comments") or 0),
            "created_utc": int(p.get("created_utc") or 0),
            "permalink": p.get("url", ""),
            "source_query": query,
            "source": "reddit_native",
        })
    # fetch_feed already paces requests via the global throttle; keep the small
    # extra gap only when a caller explicitly asks for one.
    if sleep_between > 0:
        time.sleep(sleep_between)
    return threads


def search_subreddits(query: str, limit: int = 12,
                      sleep_between: float = 0.3) -> list[dict[str, Any]]:
    """Reddit SUBREDDIT-directory search via RSS (keyless).

    Returns the COMMUNITIES whose name/description match a category term, not the
    threads matching a phrase. This is the recall source that surfaces
    r/projectmanagement / r/agency / r/<competitor>, which thread-host extraction
    misses. Each hit: {sub, display_title, rank_pos, source_query,
    source: "subreddit_search"}. The sub name is taken from the entry link
    (/r/<name>/), the only reliable source (the <title> is a display name).
    Shares the global per-IP throttle via fetch_xml_resilient.
    """
    if not query:
        return []
    encoded = urllib.parse.quote(query)
    path = f"/subreddits/search.rss?q={encoded}&limit={int(limit)}"
    root = reddit.fetch_xml_resilient(path, timeout=12)
    if root is None:
        return []
    ns = "{http://www.w3.org/2005/Atom}"
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pos, entry in enumerate(root.findall(f"{ns}entry")):
        link_el = entry.find(f"{ns}link")
        href = link_el.get("href", "") if link_el is not None else ""
        m = _SR_LINK_RE.search(href)
        if not m:
            continue
        sub = m.group(1)
        key = sub.lower()
        if not _SUBNAME_RE.match(sub) or key in seen:
            continue
        seen.add(key)
        hits.append({
            "sub": sub,
            "display_title": (entry.findtext(f"{ns}title") or "").strip(),
            "rank_pos": pos,
            "source_query": query,
            "source": "subreddit_search",
        })
    if sleep_between > 0:
        time.sleep(sleep_between)
    return hits


def search_dfs(query: str, conn) -> list[dict[str, Any]]:
    """site:reddit.com SERP via DataForSEO. Empty when DFS not configured.

    The query is prefixed with `site:reddit.com` so Google returns
    Reddit-only results; otherwise DFS does general-web SERP and we get a
    handful of off-platform mentions.
    """
    if not query:
        return []
    scoped = f'site:reddit.com "{query}"' if " " in query else f"site:reddit.com {query}"
    raw = enrich.dfs_serp_advanced(scoped, conn)
    if not raw:
        return []
    threads: list[dict[str, Any]] = []
    for item in raw.get("items", []) or []:
        url = item.get("url") or ""
        m = _DFS_SUB_RE.search(url)
        if not m:
            continue
        sub = m.group(1)
        if not _SUBNAME_RE.match(sub):
            continue
        title = item.get("title", "") or ""
        snippet = item.get("snippet", "") or ""
        # Intent gate (hybrid, same rule as Reddit native): noise-listed subs
        # require buyer-intent in title or snippet; clean subs leak through.
        sub_key = sub.lower()
        if (sub_key in NOISE_DOWNRANK_SUBS
                and not (_has_buyer_intent(title) or _has_buyer_intent(snippet))):
            continue
        threads.append({
            "sub": sub,
            "title": title,
            # DFS doesn't give us upvotes / comment counts. Treat them as
            # neutral signals; rank() handles zeros via log(1+x) gracefully.
            "score": 0,
            "num_comments": 0,
            # Approximate recency: DFS gives no thread timestamp. Treat as
            # "indexed recently" by setting now-180d so it doesn't dominate
            # or get dropped. The rank function weights this less than
            # frequency anyway.
            "created_utc": int(time.time()) - 180 * 86400,
            "permalink": url,
            "source_query": query,
            "source": "dfs",
        })
    return threads


# ─── 3. Subreddit harvesting + ranking ─────────────────────────────────


def rank_subs(threads: list[dict[str, Any]],
              user_vocab: set[str] | None = None,
              comp_tokens: set[str] | None = None,
              directory_hits: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Aggregate threads per subreddit, score, sort desc by final_score.

    Args:
        threads: harvested threads from search_reddit + search_dfs.
        user_vocab: optional set of lowercase tokens from T2/T3/T4. When
            provided, subs whose names contain any of these tokens get the
            USER_VOCAB_BONUS. This is the "topically related to what the user
            sells" signal that prevents an accounting SaaS from ranking
            r/ecommerce alongside r/Accounting.

    Returns list of {name, score, why, thread_count, sources, noise_downranked}
    sorted high-to-low. Threads older than FRESHNESS_CUTOFF_DAYS are dropped.
    Single-thread subs (< MIN_THREAD_COUNT) are dropped to kill coincidental
    lexical matches.
    """
    now = int(time.time())
    cutoff = now - FRESHNESS_CUTOFF_DAYS * 86400

    # Bucket threads by lowercase sub name (preserve original casing for display)
    buckets: dict[str, dict[str, Any]] = {}
    for t in threads:
        if int(t.get("created_utc") or 0) < cutoff:
            continue
        sub_raw = (t.get("sub") or "").strip()
        if not sub_raw:
            continue
        key = sub_raw.lower()
        b = buckets.setdefault(key, {
            "display_name": sub_raw,
            "threads": [],
            "queries": set(),
        })
        b["threads"].append(t)
        b["queries"].add(t.get("source_query", ""))

    ranked: list[dict[str, Any]] = []
    for key, b in buckets.items():
        ts = b["threads"]
        n = len(ts)
        # Hard gate 1: drop single-thread coincidental matches.
        if n < MIN_THREAD_COUNT:
            continue
        freq = len(b["queries"])
        # Hard gate 2: drop subs that don't pass relevance signal. A sub is
        # considered relevant if EITHER (a) its name shares a token with the
        # user's domain vocabulary (T2/T3/T4 keywords), OR (b) it appears in
        # the results of 2+ distinct queries (cross-query confirmation).
        # Subs that only match ONE generic query and don't share vocabulary
        # are almost always lexical noise (r/Helldivers, r/ecommerce on an
        # accounting query, etc).
        vocab_match = (user_vocab is not None
                       and _sub_matches_user_vocab(key, user_vocab))
        if not vocab_match and freq < 2:
            continue
        # Competitor signal: does any of this sub's threads name a competitor
        # brand? Deterministic, no network. Used by the skip-validation path as
        # a precision stand-in for Phase B (a sub with a real offer signal,
        # vocab OR competitor, is kept; freq-only generic-query matches are not).
        competitor_match = False
        if comp_tokens:
            for t in ts:
                if _matched_competitor(t.get("title", ""), t.get("body", "") or "", comp_tokens):
                    competitor_match = True
                    break
        # quality: average of log1p(upvotes) + log1p(comments) per thread,
        # capped at QUALITY_CEILING. Capping is critical because a single
        # viral thread can crush the cross-query frequency signal otherwise.
        quality_sum = sum(
            math.log1p(max(0, t.get("score") or 0)) +
            math.log1p(max(0, t.get("num_comments") or 0))
            for t in ts
        )
        quality = min(quality_sum / n, QUALITY_CEILING)
        # recency: mean of exp(-age_days / 365)
        recency_sum = sum(
            math.exp(-max(0, (now - (t.get("created_utc") or now))) / 86400 / 365)
            for t in ts
        )
        recency = recency_sum / n
        # Score formula (v3): frequency is the dominant signal, thread count
        # adds volume, quality + recency are tiebreakers, vocabulary-overlap
        # bonus rewards subs whose name shares tokens with what the user
        # actually sells (data-driven, not a static list).
        vocab_bonus = USER_VOCAB_BONUS if vocab_match else 0.0
        raw_score = (
            freq * 5.0                       # cross-query confirmation
            + math.log1p(n) * 2.0            # thread volume
            + quality                        # per-thread engagement, capped
            + recency * 0.5                  # mild recency tiebreaker
            + vocab_bonus
        )
        noise_mult = NOISE_DOWNRANK_FACTOR if key in NOISE_DOWNRANK_SUBS else 1.0
        final_score = raw_score * noise_mult

        # Pick the highest-raw-score thread for why-line attribution
        top = max(
            ts,
            key=lambda t: (t.get("score") or 0) + (t.get("num_comments") or 0),
        )
        top_query = top.get("source_query", "")
        plural = "s" if n != 1 else ""
        why = f"found in {n} thread{plural} matching '{top_query}'"

        ranked.append({
            "name": b["display_name"],
            "score": round(final_score, 3),
            "why": why,
            "thread_count": n,
            "freq": freq,
            "vocab_match": bool(vocab_match),
            "competitor_match": competitor_match,
            "sources": sorted({t.get("source", "") for t in ts}),
            "noise_downranked": noise_mult < 1.0,
        })

    # ─── Directory pass: fold in subreddit-search community matches ─────────
    # These are subs Reddit's own directory returned for the offer's category /
    # audience / competitor queries. They carry no threads, so they bypass the
    # thread-count and cross-query gates and earn their score from the directory
    # signal instead. A directory hit needs a relevance signal (name/title shares
    # the user's vocab, or it is a competitor community) OR an existing thread
    # entry; pure off-topic junk (r/labrador for "professional services") is
    # dropped here, and residual semantic noise is dropped by the skill review.
    if directory_hits:
        by_key = {r["name"].lower(): r for r in ranked}
        grouped: dict[str, dict[str, Any]] = {}
        for h in directory_hits:
            sub = (h.get("sub") or "").strip()
            if not sub or not _SUBNAME_RE.match(sub):
                continue
            k = sub.lower()
            g = grouped.setdefault(k, {"name": sub, "queries": set(),
                                       "pos_min": 99, "title": ""})
            g["queries"].add(h.get("source_query", ""))
            g["pos_min"] = min(g["pos_min"], int(h.get("rank_pos", 99)))
            if h.get("display_title") and not g["title"]:
                g["title"] = h["display_title"]
        for k, g in grouped.items():
            name_title = (k + " " + (g["title"] or "")).lower()
            vocab_hit = bool(user_vocab) and (
                _sub_matches_user_vocab(k, user_vocab)
                or any(tok in name_title for tok in user_vocab))
            # Competitor COMMUNITY match must be tight: the sub IS the brand's
            # community (r/Asana), not merely a sub whose name contains the brand
            # token (r/ASANA_stock, r/asana_fan_theories). Require exact match or
            # a very short suffix.
            comp_hit = bool(comp_tokens) and any(
                k == ct
                or (len(ct) >= 4 and k.startswith(ct) and k[len(ct):] in _COMMUNITY_SUFFIXES)
                or (base != ct and len(base) >= 4 and k.startswith(base)
                    and k[len(base):] in _NONEMPTY_COMMUNITY_SUFFIXES)
                for ct in comp_tokens for base in (ct.split(".")[0],))
            existing = by_key.get(k)
            if not (vocab_hit or comp_hit or existing):
                continue  # off-topic directory noise, no offer signal
            ndq = len(g["queries"])
            pos_bonus = max(0.0, (10 - min(g["pos_min"], 10)) / 10.0)
            dir_score = (
                DIRECTORY_BASE
                + DIRECTORY_QUERY_WEIGHT * (ndq - 1)
                + pos_bonus
                + (USER_VOCAB_BONUS if vocab_hit else 0.0)
                + (COMPETITOR_COMMUNITY_BONUS if comp_hit else 0.0)
            )
            if k in NOISE_DOWNRANK_SUBS:
                dir_score *= NOISE_DOWNRANK_FACTOR
            if existing:
                existing["score"] = round(existing["score"] + dir_score, 3)
                existing["directory_match"] = True
                existing["vocab_match"] = existing.get("vocab_match") or vocab_hit
                existing["competitor_match"] = existing.get("competitor_match") or comp_hit
                existing["sources"] = sorted(set(existing.get("sources", [])) | {"subreddit_search"})
            else:
                by_key[k] = {
                    "name": g["name"],
                    "score": round(dir_score, 3),
                    "why": ("competitor community" if comp_hit else
                            "community match for '%s'"
                            % (max(g["queries"], key=len) if g["queries"] else "")),
                    "thread_count": 0,
                    "freq": ndq,
                    "vocab_match": vocab_hit,
                    "competitor_match": comp_hit,
                    "directory_match": True,
                    "sources": ["subreddit_search"],
                    "noise_downranked": k in NOISE_DOWNRANK_SUBS,
                }
        ranked = list(by_key.values())

    ranked.sort(key=lambda x: x["score"], reverse=True)
    # v3: return up to PHASE_A_CANDIDATE_CAP (12); Phase B validates + final-trims to 8
    return ranked[:PHASE_A_CANDIDATE_CAP]


# ─── 4. Phase B validation + confidence scoring ────────────────────────

# Search-within-sub net. Must cover EVERYTHING the software_buyer_intent gate
# can pass: intent verbs (for the noun path's intent side) AND product nouns
# (so noun-led threads like "Best tool for document parsing" surface) AND
# "best"/"cheaper" shopping words. Every returned thread STILL runs through the
# gate, so this widens the candidate POOL without loosening precision.
_SEARCH_INTENT_TERMS = (
    'alternative', 'alternatives', 'recommendations', 'vs', 'switching',
    '"looking for"', 'replace', 'best', 'cheaper', 'migrate',
    'software', 'tool', 'platform', 'crm', 'app',
)


def _sub_search_query(comp_tokens: set[str]) -> str:
    """Build the search-within-sub query: generic buyer terms OR up to 2
    competitor brands. Multi-word brands quoted for exact-phrase match."""
    parts = list(_SEARCH_INTENT_TERMS)
    added = 0
    for c in sorted(comp_tokens, key=len, reverse=True):
        if added >= 2:
            break
        # prefer full multi-word/dotted brand names (most specific)
        if " " in c or "." in c:
            parts.append(f'"{c}"')
            added += 1
    return " OR ".join(parts)


def _fetch_candidate_posts(sub: str, comp_tokens: set[str],
                           window_hours: int) -> tuple[list[dict], bool]:
    """Fetch candidate posts for Phase B via RSS. Search-within-sub FIRST
    (search.rss finds buyer threads across the window, not just the newest 25),
    /new/.rss fallback on empty. Dedup by post id. Returns (posts, reachable).

    Reddit 403-blocks /search.json + /new.json, so RSS is the only keyless path.
    Emits the legacy post-dict keys validate_sub_freshness reads (id, created_utc,
    title, selftext, permalink); selftext/permalink map from the RSS body/url.
    Freshness is enforced downstream by created_utc against the window, so the
    old t= filter is unnecessary. Shares the global rate-limit throttle.
    """
    posts: dict[str, dict] = {}
    reachable = False

    def _ingest(feed_posts: list[dict]) -> None:
        for p in feed_posts:
            pid = p.get("id") or p.get("canonical_url") or p.get("url")
            if pid:
                posts[pid] = {
                    "id": p.get("id", ""),
                    "created_utc": int(p.get("created_utc") or 0),
                    "title": p.get("title", ""),
                    "selftext": p.get("body", ""),
                    "permalink": p.get("url", ""),
                }

    # Primary: search within the sub for buyer-intent terms.
    q = urllib.parse.quote(_sub_search_query(comp_tokens))
    search_url = (f"https://old.reddit.com/r/{sub}/search.rss?q={q}"
                  f"&restrict_sr=1&sort=new&limit=25")
    got = reddit.fetch_feed(search_url, timeout=10)
    if got is not None:
        reachable = True
        _ingest(got)

    # Fallback: /new/.rss (only if search returned nothing).
    if not posts:
        got = reddit.fetch_feed(
            f"https://old.reddit.com/r/{sub}/new/.rss?limit=25", timeout=8)
        if got is None:
            got = reddit.fetch_feed(
                f"https://www.reddit.com/r/{sub}/new/.rss?limit=25", timeout=8)
        if got is not None:
            reachable = True
            _ingest(got)

    return list(posts.values()), reachable


def validate_sub_freshness(
    sub_name: str,
    user_vocab: set[str],
    competitors: list[str],
    *,
    window_hours: int = PHASE_B_FRESH_WINDOW_HOURS,
) -> dict[str, Any]:
    """Phase B: fetch /r/<sub>/new.json (top 25) and check for fresh buyer activity.

    A sub PASSES validation if at least one post in the last `window_hours` has
    a buyer-intent token AND mentions a vocab token (from user T2/T3/T4) OR a
    competitor brand name. This proves the sub has an ACTIVE buyer conversation
    right now, not a historical one.

    Returns:
        {
          "fresh_post_count": int,            # posts in window
          "fresh_buyer_intent_count": int,    # with intent token
          "fresh_relevance_count": int,       # intent + vocab/competitor match
          "recent_thread_url": str | None,    # best fresh thread to evidence
          "recent_thread_title": str | None,
          "recent_thread_age_h": float | None,
          "passed": bool,                     # fresh_relevance_count >= 1
          "timed_out": bool,                  # Reddit unreachable or slow
          "error": str | None,
        }
    """
    result = {
        "fresh_post_count": 0,
        "fresh_buyer_intent_count": 0,
        "fresh_relevance_count": 0,
        "weighted_relevance": 0.0,
        "paths": {"competitor": 0, "noun": 0},
        "relevance_path": None,
        "recent_thread_url": None,
        "recent_thread_title": None,
        "recent_thread_age_h": None,
        "recent_thread_created_utc": None,
        "recent_thread_iso": None,
        "recent_thread_reason": None,
        "passed": False,
        "timed_out": False,
        "error": None,
    }

    sub = (sub_name or "").strip().lower()
    if not sub or not _SUBNAME_RE.match(sub):
        result["error"] = "invalid_sub_name"
        return result

    comp_tokens = _build_competitor_tokens(competitors)

    posts, reachable = _fetch_candidate_posts(sub, comp_tokens, window_hours)
    if not reachable:
        result["error"] = "fetch_failed"
        result["timed_out"] = True
        return result

    now = time.time()
    cutoff = now - window_hours * 3600
    # Skew tolerance: a post dated more than this far in the future is bad data
    # or a clock-skew artifact; never treat it as "fresh".
    future_guard = now + 3600  # 1h grace for minor clock differences

    best_fresh: dict[str, Any] | None = None  # highest-relevance fresh thread
    weighted_relevance = 0.0
    paths_seen: dict[str, int] = {"competitor": 0, "noun": 0}

    for d in posts:
        created_utc = int(d.get("created_utc") or 0)
        # Future-date guard (clock skew / bad data): skip, do not count fresh
        if created_utc > future_guard:
            continue
        if created_utc < cutoff:
            continue
        result["fresh_post_count"] += 1

        title = d.get("title", "") or ""
        body = d.get("selftext", "") or ""

        match = software_buyer_intent(title, body, comp_tokens)
        if match.passed:
            result["fresh_buyer_intent_count"] += 1
            result["fresh_relevance_count"] += 1
            weighted_relevance += match.weight
            if match.path in paths_seen:
                paths_seen[match.path] += 1
            # Track the most recent qualifying thread as evidence. Prefer a
            # stronger path; among equal paths prefer the most recent.
            age_h = (now - created_utc) / 3600.0
            better = (
                best_fresh is None
                or (match.weight > best_fresh["weight"])
                or (match.weight == best_fresh["weight"] and age_h < best_fresh["age_h"])
            )
            if better:
                permalink = d.get("permalink", "")
                if permalink and not permalink.startswith("http"):
                    permalink = f"https://reddit.com{permalink}"
                best_fresh = {
                    "url": permalink,
                    "title": title[:120],
                    "age_h": age_h,
                    "created_utc": created_utc,
                    "path": match.path,
                    "weight": match.weight,
                    "match": match,
                }

    if best_fresh:
        result["recent_thread_url"] = best_fresh["url"]
        result["recent_thread_title"] = best_fresh["title"]
        result["recent_thread_age_h"] = round(best_fresh["age_h"], 1)
        # Absolute UTC timestamp so any discrepancy is cross-checkable against
        # what Reddit shows the user directly (defensive per Dan's feedback).
        result["recent_thread_created_utc"] = best_fresh["created_utc"]
        result["recent_thread_iso"] = datetime.fromtimestamp(
            best_fresh["created_utc"], tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        result["relevance_path"] = best_fresh["path"]
        # Truthful, plain-English reason for WHY this thread was chosen.
        result["recent_thread_reason"] = build_reason(
            best_fresh["match"], best_fresh["age_h"])

    result["weighted_relevance"] = round(weighted_relevance, 3)
    result["paths"] = paths_seen
    # Pass requires at least one medium hit's worth of weighted relevance.
    result["passed"] = weighted_relevance >= 0.6
    return result


def compute_confidence(
    *,
    freq: int,
    freq_max: int,
    vocab_match: bool,
    weighted_relevance: float,
    fresh_buyer_intent_count: int,
    is_noise: bool,
) -> int:
    """Compute 0-100 confidence score from multiple signals.

    Weighted sum of normalized components, clamped to [0, 100].

    Components (all 0..1):
      freq_norm: cross-query confirmation, normalized to batch max freq
      vocab_match: name matches user vocabulary
      relevance_norm: SUM of per-thread path weights (competitor=1.0,
        noun=0.6), capped at 5. A sub where people name competitors saturates
        faster (~3 hits) than a sub with only generic-noun matches (~5 hits).
      buyer_intent_density: fresh qualifying posts / 25 (clamp 1)
      not_noise: 0 if denylisted, 1 otherwise
    """
    freq_norm = (freq / freq_max) if freq_max > 0 else 0.0
    relevance_norm = min(weighted_relevance / 5.0, 1.0)
    buyer_intent_density = min(fresh_buyer_intent_count / 25.0, 1.0)
    not_noise = 0.0 if is_noise else 1.0

    weighted = (
        0.35 * freq_norm +
        0.20 * (1.0 if vocab_match else 0.0) +
        0.25 * relevance_norm +
        0.15 * buyer_intent_density +
        0.05 * not_noise
    )
    # Noise multiplier: subs on the denylist get their confidence halved.
    # This is on top of the additive 0.05 not_noise component so noise subs
    # don't dominate even when their other signals are strong (e.g. r/SaaS
    # has vocab match + high freq for SaaS-related users).
    noise_mult = 0.5 if is_noise else 1.0
    score = round(100 * weighted * noise_mult)
    return max(0, min(100, score))


# ─── 5. Fallback + end-to-end entry point ──────────────────────────────


def _gather_inputs(homepage_url: str, conn) -> tuple[str, list[str]]:
    """Pull cached Firecrawl markdown + DFS competitors for the homepage.

    Both are cache-reads. If the warmup hasn't fired yet (or DFS/FC are
    disabled), returns empty strings/lists; derivation falls back to T2/T3/T4
    only, which is still useful.
    """
    markdown = ""
    competitors: list[str] = []

    # Firecrawl scrape cache
    if homepage_url:
        # Normalize for cache key match: enrich.fc_scrape uses validated URL
        try:
            normalized = net.validate_url(homepage_url, kind="discover homepage")
        except ValueError:
            normalized = homepage_url
        fc_key = enrich.cache_key("scrape", normalized)
        hit = store.enrich_get(conn, "firecrawl", "scrape", fc_key)
        if hit and not hit["error"]:
            try:
                payload = json.loads(hit["payload_json"])
                markdown = payload.get("markdown") or ""
            except json.JSONDecodeError:
                pass

    # DFS competitors cache. Domain-extract from homepage_url same way warmup does.
    domain = homepage_url
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
            break
    domain = domain.split("/")[0].strip().lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if domain:
        dfs_key = enrich.cache_key("competitors_domain", domain, 10)
        hit = store.enrich_get(conn, "dataforseo", "competitors_domain", dfs_key)
        if hit and not hit["error"]:
            try:
                payload = json.loads(hit["payload_json"])
                competitors = payload.get("competitors") or []
            except json.JSONDecodeError:
                pass

    return markdown, competitors


def _vertical_clarifier_prompt() -> str:
    return (
        "Could not find specific communities from your pain phrasing.\n"
        "What industry vertical do your customers work in?\n\n"
        "→  e.g. e-commerce, healthtech, fintech, legal, real estate\n"
        "→  Or 'general' if you sell horizontally."
    )


def _stale_only_clarifier_prompt() -> str:
    """Fires when Phase A found candidates but Phase B killed them all on
    freshness (no buyer activity in the discovery window). Different question
    from the vertical clarifier: the issue isn't 'who are your buyers', it's
    'where do they talk RIGHT NOW'."""
    return (
        "Found communities where this conversation has happened historically,\n"
        "but no active buyer thread in the last 7 days.\n\n"
        "→  Broaden to the last 30 days? Reply 'broaden'.\n"
        "→  Or tell me one more specific vertical / pain phrasing to refine."
    )


def discover_subs_for_profile(
    answers: dict[str, str],
    homepage_url: str,
    conn,
    *,
    vertical: str | None = None,
    deadline: float | None = None,
    extra_competitors: list[str] | None = None,
    fresh_window_hours: int = DISCOVERY_FRESH_WINDOW_HOURS,
    skip_validation: bool = False,
) -> dict[str, Any]:
    """End-to-end discovery: derive → search (DFS + Reddit native) → rank → fallback.

    Args:
        answers: dict with keys what_offering, who_to_reach, pain_quote (T2/T3/T4).
        homepage_url: the user's homepage URL (used for cache lookups).
        conn: SQLite connection (with enrichment_cache table).
        vertical: optional, set on Tier-A retry after the clarifier sub-turn.
        deadline: optional wall-clock deadline (time.time() + N). Hard-limit on
            search loop. Defaults to start + DISCOVERY_HARD_TIMEOUT_S.

    Returns:
        {
          "subs": [{name, score, why, thread_count, sources, noise_downranked}, ...],
          "queries_used": [str, ...],
          "needs_clarification": bool,
          "clarifier_prompt": str | None,
          "discovery_unreachable": bool,
          "source_mix": {"dfs": int, "reddit_native": int},
        }
    """
    start = time.time()
    if deadline is None:
        deadline = start + DISCOVERY_HARD_TIMEOUT_S

    markdown, competitors = _gather_inputs(homepage_url, conn)
    # Merge any caller-supplied competitors (e.g. Claude's WebFetch results from
    # the skill) with cached DFS competitors. Skill-supplied ones go FIRST so
    # rule 3 picks the most accurate brands.
    if extra_competitors:
        seen = {c.lower() for c in extra_competitors}
        merged = list(extra_competitors)
        merged.extend(c for c in competitors if c.lower() not in seen)
        competitors = merged
    queries = derive_queries(answers, markdown, competitors, vertical=vertical)

    # Empty / pathological input: skip directly to Tier A
    if len(queries) < 1:
        return {
            "subs": [],
            "queries_used": [],
            "needs_clarification": True,
            "clarifier_prompt": _vertical_clarifier_prompt(),
            "discovery_unreachable": False,
            "source_mix": {"dfs": 0, "reddit_native": 0},
        }

    providers = enrich.detect_providers()
    dfs_available = providers.get("dataforseo", False)
    threads: list[dict[str, Any]] = []
    source_mix = {"dfs": 0, "reddit_native": 0, "subreddit_search": 0}
    any_provider_responded = False

    for q in queries:
        if time.time() > deadline:
            _log(f"hard timeout after {len(threads)} threads")
            break

        # Reddit native (always)
        try:
            r_threads = search_reddit(q, sleep_between=0.5)
            if r_threads:
                any_provider_responded = True
            threads.extend(r_threads)
            source_mix["reddit_native"] += len(r_threads)
        except Exception as e:
            _log(f"reddit search error for '{q}': {type(e).__name__}")

        if time.time() > deadline:
            break

        # DFS (when configured)
        if dfs_available:
            try:
                d_threads = search_dfs(q, conn)
                if d_threads:
                    any_provider_responded = True
                threads.extend(d_threads)
                source_mix["dfs"] += len(d_threads)
            except Exception as e:
                _log(f"dfs search error for '{q}': {type(e).__name__}")

    # Subreddit-directory recall (the fix for "discovery returns the wrong subs"):
    # ask Reddit which COMMUNITIES match the category / audience / competitors. This
    # surfaces r/projectmanagement, r/agency, r/<competitor> that thread-host
    # extraction misses. Runs after the thread loop so it shares the deadline.
    directory_hits: list[dict[str, Any]] = []
    for sq in derive_sub_queries(answers, competitors, vertical=vertical):
        if time.time() > deadline:
            break
        try:
            hits = search_subreddits(sq, sleep_between=0.3)
            if hits:
                any_provider_responded = True
            directory_hits.extend(hits)
            source_mix["subreddit_search"] += len(hits)
        except Exception as e:
            _log(f"subreddit search error for '{sq}': {type(e).__name__}")

    user_vocab = _build_user_vocabulary(answers)
    comp_tokens = _build_competitor_tokens(competitors)
    phase_a_candidates = rank_subs(threads, user_vocab=user_vocab,
                                   comp_tokens=comp_tokens, directory_hits=directory_hits)
    phase_a_count = len(phase_a_candidates)

    discovery_unreachable = (
        not any_provider_responded
        and source_mix["reddit_native"] == 0
        and source_mix["dfs"] == 0
        and source_mix["subreddit_search"] == 0
    )

    # Phase A produced nothing, escalate to vertical clarifier (or report unreachable)
    if not phase_a_candidates:
        return {
            "subs": [],
            "dropped_subs": [],
            "queries_used": queries,
            "needs_clarification": vertical is None,
            "clarifier_prompt": _vertical_clarifier_prompt() if vertical is None else None,
            "clarifier_reason": "no_candidates" if vertical is None else None,
            "discovery_unreachable": discovery_unreachable,
            "source_mix": source_mix,
            "phase_a_count": 0,
            "phase_b_skipped": True,
        }

    # ─── Skip-validation fast path (onboarding) ────────────────────────────
    # Onboarding only needs sub NAMES + a relevance signal to confirm with the
    # user; the real /subscope-run scan at the end validates by actually
    # surfacing. So skip the per-sub Phase B freshness fetches (fewer Reddit
    # calls, faster). Relevance is the Phase A match score normalized 0-100
    # relative to the batch (no freshness => honestly lower ceiling, no chip
    # implying a verified buyer thread).
    if skip_validation:
        # Precision stand-in for Phase B, fully deterministic (no network):
        # require a REAL offer signal per sub, either the sub name matches the
        # offer vocabulary OR one of its threads names a competitor brand. A sub
        # that only reached Phase A via freq>=2 on generic query tokens (drama
        # reposts, off-topic communities that happen to contain "alternative" or
        # a pain word) has neither signal and is dropped here. This is what
        # Phase B used to filter; skipping Phase B without this gate let garbage
        # subs rank high (the QA regression).
        def _signal_tier(c: dict[str, Any]) -> int:
            d = bool(c.get("directory_match"))
            v = bool(c.get("vocab_match"))
            cm = bool(c.get("competitor_match"))
            if d and (v or cm):
                return 3   # a community Reddit's directory returned for the category/competitor
            if v and cm:
                return 2   # thread-host sub strongly on-topic (vocab + competitor)
            return 1       # single weak signal
        signaled = [c for c in phase_a_candidates
                    if c.get("vocab_match") or c.get("competitor_match")
                    or c.get("directory_match")]
        if not signaled:
            # Nothing with a real offer signal: ask for the vertical rather than
            # show freq-only noise (or nothing). Mirrors the empty-Phase-A path.
            return {
                "subs": [],
                "dropped_subs": [],
                "queries_used": queries,
                "needs_clarification": vertical is None,
                "clarifier_prompt": _vertical_clarifier_prompt() if vertical is None else None,
                "clarifier_reason": "no_candidates" if vertical is None else None,
                "discovery_unreachable": discovery_unreachable,
                "source_mix": source_mix,
                "phase_a_count": phase_a_count,
                "validation_skipped": True,
            }
        # Rank by signal tier first, then score, so confirmed category / competitor
        # communities top the list ahead of thread-host frequency noise.
        signaled.sort(key=lambda c: (_signal_tier(c), c.get("score", 0.0)), reverse=True)
        freq_max = max((c.get("freq", 0) for c in signaled), default=0)
        subs_out = []
        for c in signaled[:MAX_SUBS_RETURNED]:
            # Honest 0-100 via compute_confidence (NOT score/batch-max, which
            # always paints the top sub 100 even if it is weak). No Phase B means
            # no fresh-intent evidence, so the score carries an honest ceiling.
            rel = compute_confidence(
                freq=c.get("freq", 0),
                freq_max=freq_max,
                vocab_match=bool(c.get("vocab_match")),
                weighted_relevance=3.0 if c.get("competitor_match") else 1.0,
                fresh_buyer_intent_count=0,
                is_noise=bool(c.get("noise_downranked")),
            )
            # Floor a directory community match above thread-host frequency noise:
            # Reddit's own directory returning the sub for the category or a
            # competitor is the strongest "the ICP lives here" signal we have
            # without Phase B. Noise-listed subs are then dialed back down.
            rel = max(rel, {3: 72, 2: 50, 1: 0}[_signal_tier(c)])
            if c.get("noise_downranked"):
                rel = int(rel * 0.6)
            subs_out.append({
                "name": c["name"],
                "relevance": rel,
                "score": round(c.get("score", 0.0), 2),
                "why": c.get("why", ""),
                "thread_count": c.get("thread_count", 0),
                "sources": c.get("sources", []),
                "noise_downranked": c.get("noise_downranked", False),
                "validation_skipped": True,
            })
        subs_out.sort(key=lambda s: s["relevance"], reverse=True)
        return {
            "subs": subs_out,
            "dropped_subs": [],
            "queries_used": queries,
            "needs_clarification": False,
            "clarifier_prompt": None,
            "clarifier_reason": None,
            "discovery_unreachable": discovery_unreachable,
            "source_mix": source_mix,
            "phase_a_count": phase_a_count,
            "validation_skipped": True,
        }

    # ─── Phase B: per-candidate freshness + relevance validation ───────────
    # Budget Phase B against PHASE_B_TIMEOUT_S. Each sub takes ~0.5-1s.
    phase_b_start = time.time()
    phase_b_deadline = phase_b_start + PHASE_B_TIMEOUT_S
    _log(f"Phase B: validating {phase_a_count} candidate subs "
         f"({fresh_window_hours}h freshness gate)")

    # Pull competitor list back out of the queries for relevance check
    # (the discover_subs_for_profile signature already merged extra + DFS comps above)
    phase_b_competitors = competitors

    survived: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    timed_out_subs: list[str] = []

    # Batch-level normalization base for confidence: max distinct-query freq.
    # rank_subs does not expose freq on the candidate dicts, so recompute it per
    # sub from the original threads bucket.
    sub_to_queries: dict[str, set[str]] = {}
    for t in threads:
        s = (t.get("sub") or "").strip().lower()
        if s:
            sub_to_queries.setdefault(s, set()).add(t.get("source_query", ""))
    freqs = {s: len(qs) for s, qs in sub_to_queries.items()}
    freq_max = max(freqs.values(), default=1) or 1

    for cand in phase_a_candidates:
        if time.time() > phase_b_deadline:
            _log(f"Phase B timeout after {len(survived)+len(dropped)} subs validated")
            # Remaining unvalidated candidates fall through with freshness_unverified flag
            # We don't drop them blindly, let caller decide. For now they get a low
            # confidence + freshness_unverified=True.
            timed_out_subs.append(cand["name"])
            continue

        sub_key = cand["name"].lower()
        freshness = validate_sub_freshness(
            cand["name"],
            user_vocab=user_vocab,
            competitors=phase_b_competitors,
            window_hours=fresh_window_hours,
        )

        if freshness["timed_out"]:
            timed_out_subs.append(cand["name"])
            # Mark as unverified, keep with low confidence rather than drop
            cand["confidence"] = 0
            cand["freshness_unverified"] = True
            cand["fresh_post_count"] = 0
            cand["fresh_buyer_intent_count"] = 0
            cand["fresh_relevance_count"] = 0
            cand["weighted_relevance"] = 0.0
            cand["relevance_path"] = None
            cand["recent_thread_url"] = None
            cand["recent_thread_title"] = None
            cand["recent_thread_age_h"] = None
            cand["recent_thread_iso"] = None
            dropped.append({
                "name": cand["name"],
                "reason": "validation_unreachable",
            })
            continue

        # Attach freshness data
        cand["fresh_post_count"] = freshness["fresh_post_count"]
        cand["fresh_buyer_intent_count"] = freshness["fresh_buyer_intent_count"]
        cand["fresh_relevance_count"] = freshness["fresh_relevance_count"]
        cand["weighted_relevance"] = freshness.get("weighted_relevance", 0.0)
        cand["relevance_path"] = freshness.get("relevance_path")
        cand["recent_thread_url"] = freshness["recent_thread_url"]
        cand["recent_thread_title"] = freshness["recent_thread_title"]
        cand["recent_thread_age_h"] = freshness["recent_thread_age_h"]
        cand["recent_thread_iso"] = freshness.get("recent_thread_iso")
        cand["recent_thread_reason"] = freshness.get("recent_thread_reason")
        cand["freshness_unverified"] = False

        if not freshness["passed"]:
            dropped.append({
                "name": cand["name"],
                "reason": "no_fresh_buyer_activity",
                "fresh_post_count": freshness["fresh_post_count"],
            })
            continue

        # Compute confidence
        vocab_match = _sub_matches_user_vocab(sub_key, user_vocab)
        confidence = compute_confidence(
            freq=freqs.get(sub_key, 1),
            freq_max=freq_max,
            vocab_match=vocab_match,
            weighted_relevance=freshness.get("weighted_relevance", 0.0),
            fresh_buyer_intent_count=freshness["fresh_buyer_intent_count"],
            is_noise=cand.get("noise_downranked", False),
        )
        cand["confidence"] = confidence

        if confidence < CONFIDENCE_FLOOR:
            dropped.append({
                "name": cand["name"],
                "reason": "below_floor",
                "confidence": confidence,
            })
            continue

        survived.append(cand)

    # Sort survived by confidence desc, then by raw score desc (tiebreaker)
    survived.sort(key=lambda x: (-x["confidence"], -x.get("score", 0)))
    final_subs = survived[:MAX_SUBS_RETURNED]

    # Clarifier logic v3:
    # - stale_only: Phase A found ≥3 candidates but Phase B killed them all on freshness
    # - vertical: Phase A found <3 candidates (already handled above)
    # - low confidence: enough subs but all below threshold
    needs_clarification = False
    clarifier_reason: str | None = None
    clarifier_prompt: str | None = None

    if len(final_subs) < MIN_SUBS_THRESHOLD and vertical is None:
        # Did Phase A succeed but Phase B kill on freshness?
        freshness_drops = sum(
            1 for d in dropped if d.get("reason") == "no_fresh_buyer_activity"
        )
        if freshness_drops >= 2 and phase_a_count >= 3:
            needs_clarification = True
            clarifier_reason = "stale_only"
            clarifier_prompt = _stale_only_clarifier_prompt()
        else:
            needs_clarification = True
            clarifier_reason = "thin_results"
            clarifier_prompt = _vertical_clarifier_prompt()

    return {
        "subs": final_subs,
        "dropped_subs": dropped,
        "queries_used": queries,
        "needs_clarification": needs_clarification,
        "clarifier_prompt": clarifier_prompt,
        "clarifier_reason": clarifier_reason,
        "discovery_unreachable": discovery_unreachable,
        "source_mix": source_mix,
        "phase_a_count": phase_a_count,
        "phase_b_timed_out": timed_out_subs,
        "phase_b_skipped": False,
    }
