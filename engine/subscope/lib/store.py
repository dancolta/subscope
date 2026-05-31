"""SQLite store. Schema bootstrap + queries.

Single source of truth for dedup (surfaced.post_id PK = post is shown at most once across all time).
"""
from __future__ import annotations

import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id                TEXT PRIMARY KEY,
    subreddit         TEXT NOT NULL,
    title             TEXT NOT NULL,
    url               TEXT NOT NULL UNIQUE,
    canonical_url     TEXT NOT NULL UNIQUE,
    author            TEXT NOT NULL,
    created_utc       INTEGER NOT NULL,
    score             INTEGER NOT NULL DEFAULT 0,
    num_comments      INTEGER NOT NULL DEFAULT 0,
    body              TEXT,
    first_seen_at     INTEGER NOT NULL,
    score_internal    REAL NOT NULL DEFAULT 0.0,
    removed           INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_posts_sub_created ON posts(subreddit, created_utc DESC);
CREATE INDEX IF NOT EXISTS idx_posts_score ON posts(score_internal DESC);

CREATE TABLE IF NOT EXISTS subreddits (
    name              TEXT PRIMARY KEY,
    tier              INTEGER NOT NULL,
    bucket            TEXT NOT NULL,
    saturation        TEXT,
    weight            REAL NOT NULL DEFAULT 1.0,
    last_cursor       TEXT,
    last_run_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_subreddits_tier ON subreddits(tier);

CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        INTEGER NOT NULL,
    finished_at       INTEGER,
    posts_fetched     INTEGER NOT NULL DEFAULT 0,
    posts_surfaced    INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS blog_posts (
    url               TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    pain              TEXT NOT NULL,
    saas_replaced     TEXT NOT NULL,
    persona           TEXT NOT NULL,
    stack             TEXT NOT NULL,
    keywords          TEXT NOT NULL,
    last_refreshed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS post_blog_refs (
    post_id           TEXT NOT NULL,
    blog_url          TEXT NOT NULL,
    match_score       REAL NOT NULL,
    matched_keywords  TEXT,
    PRIMARY KEY (post_id, blog_url),
    FOREIGN KEY (post_id) REFERENCES posts(id),
    FOREIGN KEY (blog_url) REFERENCES blog_posts(url)
);
CREATE INDEX IF NOT EXISTS idx_post_blog_refs_post ON post_blog_refs(post_id);

CREATE TABLE IF NOT EXISTS surfaced (
    post_id           TEXT PRIMARY KEY,
    surfaced_on       TEXT NOT NULL,
    run_id            INTEGER NOT NULL,
    tier              INTEGER NOT NULL,
    state             TEXT NOT NULL DEFAULT 'hot',
    surfaced_at       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (post_id) REFERENCES posts(id),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_surfaced_date ON surfaced(surfaced_on);
CREATE INDEX IF NOT EXISTS idx_surfaced_state ON surfaced(state, surfaced_at);

CREATE TABLE IF NOT EXISTS enrichment_cache (
    provider          TEXT NOT NULL,
    endpoint          TEXT NOT NULL,
    key_hash          TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    fetched_at        INTEGER NOT NULL,
    expires_at        INTEGER NOT NULL,
    error             TEXT,
    PRIMARY KEY (provider, endpoint, key_hash)
);
CREATE INDEX IF NOT EXISTS idx_enrich_fresh ON enrichment_cache(provider, expires_at);
"""


def _xdg_data_dir() -> Path:
    """Resolve the user-data directory, honoring SUBSCOPE_DATA, then XDG_DATA_HOME,
    then defaulting to ~/.local/share/subscope/. Created with 0o700 perms if missing.

    Env precedence (highest first):
      1. SUBSCOPE_DATA      — project override (set by tests / CI)
      2. XDG_DATA_HOME           — XDG Base Directory spec
      3. ~/.local/share          — XDG default
    """
    if override := os.environ.get("SUBSCOPE_DATA"):
        d = Path(override).expanduser()
    elif xdg := os.environ.get("XDG_DATA_HOME"):
        d = Path(xdg).expanduser() / "subscope"
    else:
        d = Path.home() / ".local" / "share" / "subscope"
    d.mkdir(parents=True, exist_ok=True)
    # 0o700: owner-only. Defends API credentials cached in companion files.
    try:
        d.chmod(stat.S_IRWXU)
    except OSError:
        # Best-effort; on some filesystems (NFS, FAT) chmod is a noop.
        pass
    return d


def xdg_config_dir() -> Path:
    """Resolve the user-config directory. Same precedence as _xdg_data_dir() but
    rooted at XDG_CONFIG_HOME / ~/.config."""
    if override := os.environ.get("SUBSCOPE_CONFIG"):
        d = Path(override).expanduser()
    elif xdg := os.environ.get("XDG_CONFIG_HOME"):
        d = Path(xdg).expanduser() / "subscope"
    else:
        d = Path.home() / ".config" / "subscope"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(stat.S_IRWXU)
    except OSError:
        pass
    return d


def db_path(project_root: Path | str | None = None) -> Path:
    """Resolve SQLite DB path.

    Default (production): `<xdg_data>/subscope.sqlite` (see `_xdg_data_dir`).
    Override (legacy / tests): pass `project_root` explicitly → `<project_root>/db/subscope.sqlite`.
    """
    if project_root is not None:
        return Path(project_root) / "db" / "subscope.sqlite"
    return _xdg_data_dir() / "subscope.sqlite"


@contextmanager
def connect(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    new_db = not p.exists() or p.stat().st_size == 0
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if new_db:
        # First connect to a fresh DB → install schema. Idempotent because
        # SCHEMA uses CREATE TABLE IF NOT EXISTS everywhere.
        conn.executescript(SCHEMA)
        conn.commit()
        # Owner-only perms on the DB itself. The XDG dir is 0o700 already,
        # but defense-in-depth on shared boxes (or if user moved DB under
        # SUBSCOPE_DATA to a more permissive location).
        try:
            os.chmod(str(p), 0o600)
        except OSError:
            pass
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bootstrap(path: Path | str | None = None) -> None:
    # Kept for explicit-setup callers. `connect()` now auto-bootstraps on
    # first contact with an empty DB, but `setup` still calls this for clarity.
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def upsert_subreddit(conn: sqlite3.Connection, sub: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO subreddits(name, tier, bucket, saturation, weight)
           VALUES(?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               tier=excluded.tier, bucket=excluded.bucket,
               saturation=excluded.saturation, weight=excluded.weight""",
        (sub["name"], sub["tier"], sub["bucket"],
         sub.get("saturation"), sub.get("weight", 1.0)),
    )


def upsert_blog_post(conn: sqlite3.Connection, blog: dict[str, Any]) -> None:
    kw = "|".join(blog.get("keywords", []))
    conn.execute(
        """INSERT INTO blog_posts(url, title, pain, saas_replaced, persona, stack, keywords, last_refreshed_at)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET
               title=excluded.title, pain=excluded.pain, saas_replaced=excluded.saas_replaced,
               persona=excluded.persona, stack=excluded.stack, keywords=excluded.keywords,
               last_refreshed_at=excluded.last_refreshed_at""",
        (blog["url"], blog["title"], blog["pain"], blog["saas_replaced"],
         blog["persona"], blog["stack"], kw, int(time.time())),
    )


def already_surfaced(conn: sqlite3.Connection, post_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM surfaced WHERE post_id = ?", (post_id,)).fetchone()
    return row is not None


def insert_post(conn: sqlite3.Connection, post: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO posts
           (id, subreddit, title, url, canonical_url, author, created_utc,
            score, num_comments, body, first_seen_at, score_internal, removed)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (post["id"], post["subreddit"], post["title"], post["url"],
         post["canonical_url"], post["author"], post["created_utc"],
         post["score"], post["num_comments"], post.get("body", ""),
         int(time.time()), post.get("score_internal", 0.0),
         1 if post.get("removed") else 0),
    )


def update_score(conn: sqlite3.Connection, post_id: str, score_internal: float) -> None:
    conn.execute("UPDATE posts SET score_internal = ? WHERE id = ?", (score_internal, post_id))


def record_blog_ref(conn: sqlite3.Connection, post_id: str, blog_url: str,
                    match_score: float, matched_keywords: list[str]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO post_blog_refs(post_id, blog_url, match_score, matched_keywords)
           VALUES(?, ?, ?, ?)""",
        (post_id, blog_url, match_score, "|".join(matched_keywords)),
    )


def _ensure_surfaced_state_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add `state` + `surfaced_at` columns to surfaced
    if they don't exist. Cooling queue requires both.

    state: 'drafting' (cooling) | 'hot' (visible) | 'dead' (decayed/archived)
    surfaced_at: unix seconds (granular enough to compute age in minutes)
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(surfaced)").fetchall()}
    if "state" not in cols:
        conn.execute("ALTER TABLE surfaced ADD COLUMN state TEXT NOT NULL DEFAULT 'hot'")
    if "surfaced_at" not in cols:
        conn.execute("ALTER TABLE surfaced ADD COLUMN surfaced_at INTEGER NOT NULL DEFAULT 0")


def mark_surfaced(
    conn: sqlite3.Connection,
    post_id: str,
    run_id: int,
    tier: int,
    state: str = "drafting",
) -> None:
    """Insert a surfaced row. New posts default to 'drafting' (cooling queue);
    pass state='hot' to bypass cooling for time-sensitive patterns like pricing-rage."""
    _ensure_surfaced_state_column(conn)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    conn.execute(
        """INSERT INTO surfaced(post_id, surfaced_on, run_id, tier, state, surfaced_at)
           VALUES(?, ?, ?, ?, ?, ?)""",
        (post_id, today, run_id, tier, state, int(time.time())),
    )


def flush_cooling_queue(conn: sqlite3.Connection, cool_minutes: int = 30) -> int:
    """Promote drafting rows older than `cool_minutes` to 'hot'.

    Returns count of rows promoted. Idempotent: re-running on the same DB
    after another `cool_minutes` flushes a fresh batch.
    """
    _ensure_surfaced_state_column(conn)
    threshold = int(time.time()) - (cool_minutes * 60)
    cur = conn.execute(
        "UPDATE surfaced SET state = 'hot' "
        "WHERE state = 'drafting' AND surfaced_at <= ?",
        (threshold,),
    )
    return cur.rowcount


def hot_surfaces(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch surfaces currently in 'hot' state (post-cooling, pre-decay).
    Joined with posts for the most common consumer query (Notion sync)."""
    _ensure_surfaced_state_column(conn)
    rows = conn.execute(
        "SELECT s.post_id, s.surfaced_on, s.tier, s.state, s.surfaced_at, "
        "       p.title, p.url, p.canonical_url, p.subreddit, p.score, "
        "       p.num_comments, p.body, p.score_internal "
        "FROM surfaced s JOIN posts p ON p.id = s.post_id "
        "WHERE s.state = 'hot' ORDER BY p.score_internal DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def decay_old_surfaces(conn: sqlite3.Connection, days: int = 14) -> int:
    """Mark hot surfaces older than `days` as 'dead'. Returns rows decayed."""
    _ensure_surfaced_state_column(conn)
    threshold = int(time.time()) - (days * 86400)
    cur = conn.execute(
        "UPDATE surfaced SET state = 'dead' "
        "WHERE state IN ('hot', 'drafting') AND surfaced_at <= ?",
        (threshold,),
    )
    return cur.rowcount


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO runs(started_at) VALUES(?)", (int(time.time()),)
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int,
               posts_fetched: int, posts_surfaced: int, notes: str = "") -> None:
    conn.execute(
        """UPDATE runs SET finished_at = ?, posts_fetched = ?, posts_surfaced = ?, notes = ?
           WHERE id = ?""",
        (int(time.time()), posts_fetched, posts_surfaced, notes, run_id),
    )


def update_cursor(conn: sqlite3.Connection, sub_name: str, cursor: str) -> None:
    conn.execute(
        "UPDATE subreddits SET last_cursor = ?, last_run_at = ? WHERE name = ?",
        (cursor, int(time.time()), sub_name),
    )


def fetch_blog_posts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM blog_posts").fetchall()
    return [dict(r) for r in rows]


def get_sub(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM subreddits WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def stats_last_run(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else {}


def _ensure_enrichment_cache_table(conn: sqlite3.Connection) -> None:
    """Idempotent migration for the enrichment_cache table.

    Existing DBs (created before the DFS+Firecrawl wiring) lack this table.
    Call this lazily from every enrich_* helper so the table appears on first
    use, no upgrade script required.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS enrichment_cache (
            provider          TEXT NOT NULL,
            endpoint          TEXT NOT NULL,
            key_hash          TEXT NOT NULL,
            payload_json      TEXT NOT NULL,
            fetched_at        INTEGER NOT NULL,
            expires_at        INTEGER NOT NULL,
            error             TEXT,
            PRIMARY KEY (provider, endpoint, key_hash)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_enrich_fresh "
        "ON enrichment_cache(provider, expires_at)"
    )


def enrich_get(
    conn: sqlite3.Connection,
    provider: str,
    endpoint: str,
    key_hash: str,
) -> dict[str, Any] | None:
    """Read a cached enrichment row. Returns None on miss OR on expiry.

    A non-null `error` column signals a negative-cached failure; callers can
    inspect `row["error"]` to short-circuit without re-fetching during the
    backoff window.
    """
    _ensure_enrichment_cache_table(conn)
    row = conn.execute(
        "SELECT payload_json, fetched_at, expires_at, error "
        "FROM enrichment_cache "
        "WHERE provider = ? AND endpoint = ? AND key_hash = ?",
        (provider, endpoint, key_hash),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= int(time.time()):
        return None
    return dict(row)


def enrich_put(
    conn: sqlite3.Connection,
    provider: str,
    endpoint: str,
    key_hash: str,
    payload: str,
    ttl_seconds: int,
    error: str | None = None,
) -> None:
    """Upsert a cache row. `payload` is the serialized JSON string (caller
    json.dumps's it). `error` non-null marks the row as a negative cache;
    set a short TTL in that case.
    """
    _ensure_enrichment_cache_table(conn)
    now = int(time.time())
    conn.execute(
        """INSERT INTO enrichment_cache
               (provider, endpoint, key_hash, payload_json,
                fetched_at, expires_at, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(provider, endpoint, key_hash) DO UPDATE SET
               payload_json = excluded.payload_json,
               fetched_at   = excluded.fetched_at,
               expires_at   = excluded.expires_at,
               error        = excluded.error""",
        (provider, endpoint, key_hash, payload,
         now, now + ttl_seconds, error),
    )


def enrich_purge_expired(conn: sqlite3.Connection) -> int:
    """Delete expired rows. Returns count purged.

    Cheap maintenance: called opportunistically (e.g. once per scan) so the
    cache table stays bounded.
    """
    _ensure_enrichment_cache_table(conn)
    cur = conn.execute(
        "DELETE FROM enrichment_cache WHERE expires_at <= ?",
        (int(time.time()),),
    )
    return cur.rowcount
