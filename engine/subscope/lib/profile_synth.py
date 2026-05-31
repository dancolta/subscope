"""Profile synthesis: turn interview answers into a personalized subscope config.

Used by `/subscope-profile` and `/subscope-onboard`. Two modes:

  1. Archetype-mapped synthesis (the default path): Claude reasons over the
     interview answers in chat and layers the user's competitors, pain language,
     and homepage findings on top of an archetype seed. The in-session reasoning
     IS the synthesis.
  2. Pure archetype fallback (no LLM reasoning available): the interview answers
     are used only to pick the closest of 6 pre-baked archetypes.

Output: 4 YAML files written under XDG config dir:
  - subreddits.yml     (tier 1/2/3 with reasoning per sub)
  - keywords.yml       (shared/operator/builder buckets)
  - brand-anchor.yml   (competitor list)
  - example-pains.yml  (5 example pain titles for the optional LLM classifier)

Plus a validation pass that enforces caps from weights.yml config_ceilings.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import archetype_map, store


# ─── JSON schema for LLM output (validated before YAML emit) ──────────
SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "tier_1", "tier_2", "tier_3", "candidates_to_verify",
        "keywords", "brand_anchor", "example_pains",
    ],
    "properties": {
        "tier_1": {"type": "array", "minItems": 3, "maxItems": 5},
        "tier_2": {"type": "array", "minItems": 3, "maxItems": 12},
        "tier_3": {"type": "array"},
        "candidates_to_verify": {"type": "array"},
        "keywords": {
            "type": "object",
            "required": ["shared", "operator", "builder"],
            "properties": {
                "shared": {"type": "array", "minItems": 3},
                "operator": {"type": "array", "minItems": 3},
                "builder": {"type": "array", "minItems": 3},
            },
        },
        "brand_anchor": {"type": "array", "minItems": 8, "maxItems": 25},
        "example_pains": {"type": "array", "minItems": 5, "maxItems": 5},
    },
}


# Phrases that are too generic to live in a personalized keyword bucket.
# The base engine adds these as baseline; the profile must produce SHARP
# ICP-specific phrases, not these.
BANNED_GENERIC_KEYWORDS = {
    "alternative to", "switching from", "looking for a tool",
    "anyone use", "recommendations for", "what's the best",
    "any good", "recommended",
}

# 2026 universal watch list — never tier 1 or tier 2 regardless of synthesis.
# Mirror of archetype_map.UNIVERSAL_WATCH_LIST plus a few extras observed
# during Phase -1 reddit-community-builder research.
WATCH_LIST = archetype_map.UNIVERSAL_WATCH_LIST | {
    "entrepreneurridealong",
}


def validate_synthesis(payload: dict[str, Any], weights_cfg: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    """Return (ok, problems). Used between LLM output and disk write."""
    problems: list[str] = []
    ceilings = (weights_cfg or {}).get("config_ceilings", {})

    # Required fields
    for key in SYNTHESIS_SCHEMA["required"]:
        if key not in payload:
            problems.append(f"missing required field: {key}")
    if problems:
        return False, problems

    # Tier 1 cap (hard requirement)
    t1_max = int(ceilings.get("tier1_subs_max", 5))
    if len(payload["tier_1"]) > t1_max:
        problems.append(f"tier_1 has {len(payload['tier_1'])} subs, max {t1_max}")

    # Tier 2 ceiling
    t2_max = int(ceilings.get("tier2_subs_max", 8))
    if len(payload["tier_2"]) > t2_max:
        problems.append(f"tier_2 has {len(payload['tier_2'])} subs, max {t2_max}")

    # No duplicate sub names across tiers
    seen: dict[str, str] = {}
    for tier_name in ("tier_1", "tier_2", "tier_3"):
        for entry in payload[tier_name]:
            name = (entry.get("name") or "").lower()
            if not name:
                problems.append(f"{tier_name} entry missing name: {entry}")
                continue
            if name in seen:
                problems.append(f"sub r/{name} appears in both {seen[name]} and {tier_name}")
            seen[name] = tier_name

    # Watch list — any sub in WATCH_LIST must NOT be in tier 1/2
    for tier_name in ("tier_1", "tier_2"):
        for entry in payload[tier_name]:
            name = (entry.get("name") or "").lower()
            if name in {w.lower() for w in WATCH_LIST}:
                problems.append(f"r/{name} is on 2026 watch list, cannot be in {tier_name}")

    # Every sub in tier_1/tier_2 has a reason field referencing interview
    for tier_name in ("tier_1", "tier_2"):
        for entry in payload[tier_name]:
            reason = entry.get("reason") or ""
            if len(reason) < 15:
                problems.append(f"{tier_name} r/{entry.get('name','?')} missing or short reason field")

    # Weight bounds
    for tier_name in ("tier_1", "tier_2"):
        for entry in payload[tier_name]:
            w = entry.get("weight")
            if w is None or not 0.0 <= float(w) <= 2.0:
                problems.append(f"{tier_name} r/{entry.get('name','?')} weight {w} out of [0,2]")

    # Bucket field present
    for tier_name in ("tier_1", "tier_2"):
        for entry in payload[tier_name]:
            if entry.get("bucket") not in ("operator", "builder"):
                problems.append(f"{tier_name} r/{entry.get('name','?')} bucket must be operator|builder")

    # Keyword anti-generic filter
    for bucket_name in ("shared", "operator", "builder"):
        kws = payload["keywords"].get(bucket_name) or []
        for kw in kws:
            kw_lower = kw.lower().strip()
            if any(banned in kw_lower for banned in BANNED_GENERIC_KEYWORDS):
                problems.append(f"keywords.{bucket_name} contains generic phrase: '{kw}'")

    # Keyword bucket size ceiling
    kw_max = int(ceilings.get("keywords_per_bucket_max", 50))
    for bucket_name in ("shared", "operator", "builder"):
        n = len(payload["keywords"].get(bucket_name) or [])
        if n > kw_max:
            problems.append(f"keywords.{bucket_name} has {n} entries, max {kw_max}")

    # Brand anchor bounds
    ba = payload.get("brand_anchor") or []
    ba_max = int(ceilings.get("brand_anchor_max", 20))
    if len(ba) > ba_max:
        problems.append(f"brand_anchor has {len(ba)} entries, max {ba_max}")
    if len(ba) < 8:
        problems.append(f"brand_anchor has {len(ba)} entries, min 8")

    # Example pains — must be 5 strings, 20-160 chars each
    pains = payload.get("example_pains") or []
    if len(pains) != 5:
        problems.append(f"example_pains must have exactly 5 entries, got {len(pains)}")
    for p in pains:
        if not isinstance(p, str) or not 20 <= len(p) <= 160:
            problems.append(f"example_pain length out of [20,160]: '{p[:40]}...'")

    return (len(problems) == 0, problems)


def to_yaml_files(payload: dict[str, Any]) -> dict[str, str]:
    """Convert a validated synthesis payload into 4 commented YAML strings.

    Returns {filename: content}. Caller writes to XDG config dir.
    """
    # subreddits.yml — commented per-sub
    subs_lines = [
        "# Generated by /subscope-profile.",
        "# Edit freely; re-running /profile preserves your manual edits.",
        "#",
        "# Every /subscope-run scans these subs and sorts what it finds into two",
        "# tracks: Buyer signals (a reply could move a deal) and Authority plays",
        "# (a helpful reply builds credibility). A sub is not one or the other; it",
        "# can produce both. 'weight' is how strongly a sub is prioritised in",
        "# ranking (higher = more relevant to your offer).",
        "",
    ]
    subs_lines.append("subreddits:")

    def _emit_tier(label: str, entries: list[dict[str, Any]], tier_num: int):
        if not entries:
            return
        subs_lines.append(f"")
        subs_lines.append(f"  # === Tier {tier_num} {label} ===")
        for e in entries:
            reason = (e.get("reason") or "").strip().replace("\n", " ")[:80]
            if reason:
                subs_lines.append(f"  # {reason}")
            tail_parts = [
                f"name: {e['name']}",
                f"tier: {tier_num}",
                f"bucket: {e.get('bucket', 'operator')}",
                f"weight: {float(e.get('weight', 1.0))}",
            ]
            if e.get("saturation"):
                tail_parts.append(f"saturation: {e['saturation']}")
            subs_lines.append(f"  - {{{', '.join(tail_parts)}}}")

    _emit_tier("Primary (scanned every run)", payload["tier_1"], 1)
    _emit_tier("Secondary (opportunistic)", payload["tier_2"], 2)
    if payload.get("tier_3"):
        subs_lines.append("")
        subs_lines.append("  # === Tier 3 quarantined (fetched for telemetry, never surfaced) ===")
        for e in payload["tier_3"]:
            reason = (e.get("reason") or "").strip().replace("\n", " ")[:80]
            if reason:
                subs_lines.append(f"  # {reason}")
            subs_lines.append(
                f"  - {{name: {e['name']}, tier: 3, bucket: operator, weight: 0.0}}"
            )
    subs_yaml = "\n".join(subs_lines) + "\n"

    # keywords.yml
    kw = payload["keywords"]
    kw_lines = ["# Generated by /subscope-profile.",
                "# Buckets: shared = both audiences; operator = ICP work-pain;",
                "#          builder = founder/dev/indie-hacker shop-talk.", ""]
    for bucket in ("shared", "operator", "builder"):
        kw_lines.append(f"{bucket}:")
        for k in (kw.get(bucket) or []):
            kw_lines.append(f"  - {json.dumps(k)}")
        kw_lines.append("")
    kw_yaml = "\n".join(kw_lines)

    # brand-anchor.yml
    ba_lines = [
        "# Competitors + adjacent SaaS the ICP touches. Used to anchor surface",
        "# scoring — a post that names one of these gets a brand-anchor boost.",
        "",
        "brand_anchor:",
    ]
    for b in payload["brand_anchor"]:
        ba_lines.append(f"  - {json.dumps(b)}")
    ba_yaml = "\n".join(ba_lines) + "\n"

    # example-pains.yml (for LLM classifier few-shots)
    ex_lines = [
        "# Example pain-post titles in the voice of your ICP.",
        "# Used as few-shot examples for the optional LLM classifier (Phase 2).",
        "",
        "example_pains:",
    ]
    for p in payload["example_pains"]:
        ex_lines.append(f"  - {json.dumps(p)}")
    ex_yaml = "\n".join(ex_lines) + "\n"

    return {
        "subreddits.yml": subs_yaml,
        "keywords.yml": kw_yaml,
        "brand-anchor.yml": ba_yaml,
        "example-pains.yml": ex_yaml,
    }


def write_to_xdg(yaml_files: dict[str, str], backup: bool = True) -> dict[str, Path]:
    """Write the 4 YAML files to ~/.config/subscope/. Returns paths written.

    If `backup` and the target exists, copy to `<name>.bak.<timestamp>` first.
    """
    import shutil
    import time
    config_dir = store.xdg_config_dir()
    written: dict[str, Path] = {}
    for filename, content in yaml_files.items():
        path = config_dir / filename
        if backup and path.exists():
            ts = int(time.time())
            shutil.copy2(path, path.with_suffix(path.suffix + f".bak.{ts}"))
        path.write_text(content)
        path.chmod(0o600)
        written[filename] = path
    return written


def fallback_from_archetype(interview_answers: dict[str, str]) -> dict[str, Any]:
    """Build a synthesis payload from archetype_map when LLM is unavailable.

    Less personalized than LLM synthesis but always produces a usable config.
    Used as the "no API key" path AND as a last-resort fallback when LLM
    synthesis fails validation.
    """
    arch_key = archetype_map.best_match(interview_answers)
    arch = archetype_map.get(arch_key)
    assert arch is not None, f"archetype {arch_key} not found"

    # Coerce archetype shape → synthesis schema shape
    tier_1 = [
        {**entry, "reason": f"Archetype '{arch_key}': {arch['label']}"}
        for entry in arch["tier_1"]
    ]
    tier_2 = [
        {
            "name": name, "weight": 1.0, "bucket": "operator",
            "saturation": "medium",
            "reason": f"Archetype '{arch_key}': tier-2 candidate",
        }
        for name in arch["tier_2"]
    ]
    tier_3 = [
        {"name": name, "reason": f"Archetype '{arch_key}': quarantined"}
        for name in arch.get("quarantine", [])
    ]

    # Best-effort keyword seeds from interview answers
    pain_quote = (interview_answers.get("pain_quote") or "").lower()
    competitors_raw = interview_answers.get("competitors") or ""
    competitors = [c.strip() for c in competitors_raw.split(",") if c.strip()]

    keywords = {
        "shared": ["pain", "stuck", "broken", "expensive", "frustrating"],
        "operator": ["workflow", "stack", "tool sprawl", "ROI", "switching from"],
        "builder": ["build vs buy", "rolled our own", "internal tool"],
    }
    if pain_quote:
        keywords["shared"].insert(0, pain_quote[:80])

    return {
        "tier_1": tier_1,
        "tier_2": tier_2,
        "tier_3": tier_3,
        "candidates_to_verify": [],
        "keywords": keywords,
        "brand_anchor": competitors[:12] if len(competitors) >= 8 else (
            competitors + ["HubSpot", "Salesforce", "Slack", "Notion", "Linear",
                           "Stripe", "Vercel", "Supabase"][: 12 - len(competitors)]
        ),
        "example_pains": [
            f"Stuck on: {pain_quote[:100] or 'finding alternatives that actually work for our team'}",  # noqa: E501
            "Anyone else seeing pricing creep at renewal time?",
            "We rolled our own internal tool — is this insane or normal?",
            "What's the right time to switch from a no-code stack?",
            "Looking for someone who has actually shipped this end-to-end",
        ],
    }


# ─── /subscope-onboard helpers (3-question light flow) ─────────────────────
# Folded in from the old onboard_synth.py module — those 5 helpers were 80%
# pass-through to functions already in this file. Keeping the same names so
# external callers don't break.

OnboardAnswers = dict[str, str]
"""The 3 answers come in as a flat dict: who_to_reach, what_offering, homepage_url."""


# Reuse this module's validator (same shape requirements as the 8-Q flow)
validate = validate_synthesis


def archetype_seed(answers: OnboardAnswers) -> dict[str, Any]:
    """Best-effort archetype-mapped seed for the 3-question /subscope-onboard flow.

    Claude reasons over the 3 answers in chat (free, subscription path); this
    helper gives it a baseline payload to layer the user's specific
    competitors / pain language / homepage findings on top of.
    """
    return fallback_from_archetype({
        "what_you_sell": answers.get("what_offering", ""),
        "icp": answers.get("who_to_reach", ""),
        "pain_quote": (answers.get("who_to_reach", "") + " "
                       + answers.get("what_offering", "")),
        "competitors": "",
    })


def merge_url_extracts(payload: dict[str, Any], url_extracts: dict[str, Any]) -> dict[str, Any]:
    """Merge homepage-URL-derived intelligence into the synthesis payload.

    The /subscope-onboard skill WebFetches the user's homepage and pulls:
      - H1 + tagline
      - first 400 chars of body
      - any visible competitor logos (if Claude can identify them)
      - pricing tier signal

    This function amplifies the relevant payload fields with those extracts
    (more competitors in brand_anchor, sharper keywords).
    """
    if not url_extracts:
        return payload

    # Append URL-extracted competitors to brand_anchor (dedup)
    new_competitors = url_extracts.get("competitors") or []
    existing = {b.lower() for b in payload.get("brand_anchor", [])}
    for comp in new_competitors:
        if comp.lower() not in existing:
            payload.setdefault("brand_anchor", []).append(comp)
            existing.add(comp.lower())
    # Cap at sweet spot
    payload["brand_anchor"] = payload.get("brand_anchor", [])[:20]

    # Append URL-extracted pain phrases to shared keywords
    new_pain = url_extracts.get("pain_phrases") or []
    existing_kw = {k.lower() for k in payload.get("keywords", {}).get("shared", [])}
    for phrase in new_pain:
        if phrase.lower() not in existing_kw and len(phrase) < 80:
            payload.setdefault("keywords", {}).setdefault("shared", []).append(phrase)
            existing_kw.add(phrase.lower())

    return payload


def save_draft(answers: OnboardAnswers, draft_name: str = ".onboard-draft.json") -> Path:
    """Persist the answers so /onboard is resumable mid-interview."""
    draft_path = store.xdg_config_dir() / draft_name
    draft_path.write_text(json.dumps(answers, indent=2))
    draft_path.chmod(0o600)
    return draft_path


def load_draft(draft_name: str = ".onboard-draft.json") -> OnboardAnswers | None:
    draft_path = store.xdg_config_dir() / draft_name
    if not draft_path.exists():
        return None
    try:
        return json.loads(draft_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear_draft(draft_name: str = ".onboard-draft.json") -> None:
    draft_path = store.xdg_config_dir() / draft_name
    if draft_path.exists():
        draft_path.unlink()
