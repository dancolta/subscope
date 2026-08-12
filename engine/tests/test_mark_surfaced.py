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
