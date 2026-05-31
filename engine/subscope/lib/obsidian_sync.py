"""Obsidian pulse-digest emitter.

This module does NOT write to Obsidian directly — that's done via the
obsidian MCP from inside a Claude Code session (skills/pulse/SKILL.md
orchestrates the actual write). What this module does is build the
markdown digest content from SQLite state.

Output shape:
    ---
    tags: [subscope, pulse, week-NN]
    date: YYYY-MM-DD
    total_surfaces: N
    ---

    # Week NN pulse — YYYY-MM-DD

    ## Sub × keyword heat

    | Subreddit | Top keyword | Surfaces this week | Δ vs last week |
    |---|---|---|---|

    ## Patterns that worked

    | Pattern | Surfaces | Replied | Avg upvotes 7d | Verdict |
    |---|---|---|---|---|

    ## Notes

    Generated from subscope DB on YYYY-MM-DD HH:MM UTC.
"""
from __future__ import annotations

import datetime as dt
import sqlite3


def _week_iso(now: dt.datetime | None = None) -> tuple[int, int]:
    n = now or dt.datetime.now(dt.timezone.utc)
    return n.isocalendar()[:2]  # (year, week_number)


def build_weekly_digest(conn: sqlite3.Connection, now: dt.datetime | None = None) -> str:
    """Return a complete markdown digest as a string. Caller writes it to
    Obsidian via the MCP. Never writes to filesystem itself."""
    now = now or dt.datetime.now(dt.timezone.utc)
    year, week = _week_iso(now)
    today_str = now.strftime("%Y-%m-%d")
    week_start = int((now - dt.timedelta(days=7)).timestamp())

    # Total surfaces this week
    total = conn.execute(
        "SELECT COUNT(*) FROM surfaced WHERE surfaced_at >= ?", (week_start,)
    ).fetchone()[0]

    # Per-subreddit count
    sub_rows = conn.execute(
        "SELECT p.subreddit, COUNT(*) as n "
        "FROM surfaced s JOIN posts p ON p.id = s.post_id "
        "WHERE s.surfaced_at >= ? "
        "GROUP BY p.subreddit ORDER BY n DESC LIMIT 20",
        (week_start,),
    ).fetchall()

    # Tier × count breakdown
    pattern_rows = []
    try:
        pattern_rows = conn.execute(
            "SELECT s.tier, COUNT(*) "
            "FROM surfaced s "
            "WHERE s.surfaced_at >= ? "
            "GROUP BY s.tier", (week_start,)
        ).fetchall()
    except sqlite3.Error:
        pass

    lines: list[str] = []
    lines.append("---")
    lines.append(f"tags: [subscope, pulse, week-{week:02d}]")
    lines.append(f"date: {today_str}")
    lines.append(f"total_surfaces: {total}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Week {week:02d} pulse, {today_str}")
    lines.append("")
    lines.append(f"**Total surfaces this week:** {total}")
    lines.append("")
    lines.append("## Sub × surface count")
    lines.append("")
    if sub_rows:
        lines.append("| Subreddit | Surfaces |")
        lines.append("|---|---|")
        for sub, n in sub_rows:
            lines.append(f"| r/{sub} | {n} |")
    else:
        lines.append("_No surfaces this week._")
    lines.append("")

    if pattern_rows:
        lines.append("## By tier")
        lines.append("")
        lines.append("| Tier | Count |")
        lines.append("|---|---|")
        for tier, n in pattern_rows:
            lines.append(f"| {tier} | {n} |")
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append(f"Generated from subscope DB at {dt.datetime.now(dt.timezone.utc).isoformat()}.")
    return "\n".join(lines) + "\n"


def suggested_filename(now: dt.datetime | None = None) -> str:
    """Standard filename for the weekly digest: `YYYY-WNN-pulse.md`."""
    n = now or dt.datetime.now(dt.timezone.utc)
    year, week = _week_iso(n)
    return f"{year}-W{week:02d}-pulse.md"
