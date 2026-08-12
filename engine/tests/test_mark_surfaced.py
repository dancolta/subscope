"""mark-surfaced: the judge's picks must write back to the dedup table.

fetch-score records only the engine's own lexical-gate picks. Under judge-first
the surfacing decision belongs to the skill, so without this write-back a
profile whose lookback window overlaps its run gap (any fixed schedule) re-shows
the same threads every run.
"""
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subscope import cli  # noqa: E402
from subscope.lib import store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSCOPE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("SUBSCOPE_CONFIG", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    store.bootstrap()


def _run(ids, **kw) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.cmd_mark_surfaced(ids, **kw)
    return json.loads(buf.getvalue().strip())


def test_marks_ids_so_a_later_run_skips_them():
    out = _run(["abc123", "def456"])
    assert out["marked"] == 2
    with store.connect() as conn:
        assert store.already_surfaced(conn, "abc123")
        assert store.already_surfaced(conn, "def456")


def test_is_idempotent():
    """A double call must not raise on the post_id primary key."""
    _run(["abc123"])
    out = _run(["abc123", "new999"])
    assert out["marked"] == 1
    assert out["skipped"] == 1


def test_ignores_blank_ids():
    out = _run(["abc123", "", "   "])
    assert out["marked"] == 1

def test_space_joined_ids_are_split_not_stored_whole():
    """zsh does not word-split an unquoted "$IDS", so all ids arrive as ONE
    argument. Storing that verbatim poisons dedup with a row no post can match
    and leaves every id in it un-deduped."""
    out = _run(["abc123 def456 ghi789"])
    assert out["marked"] == 3
    with store.connect() as conn:
        assert store.already_surfaced(conn, "def456")


def test_non_id_values_are_rejected_not_stored():
    """A pasted URL is the likely mistake. Reddit ids are base36, so anything
    carrying a slash, colon or punctuation can never match a real post."""
    out = _run(["abc123", "https://reddit.com/r/x/comments/abc/", "not-an-id"])
    assert out["marked"] == 1
    assert len(out["invalid"]) == 2
    with store.connect() as conn:
        assert store.already_surfaced(conn, "abc123")


def test_t3_prefix_is_stripped():
    """candidates[] carries bare ids, but the Atom feed uses t3_. Both must
    land on the same row or dedup silently misses."""
    _run(["t3_abc123"])
    with store.connect() as conn:
        assert store.already_surfaced(conn, "abc123")
