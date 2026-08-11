"""Shared test fixtures.

Keeps the suite off the network. `cmd_fetch_score` primes a combined
/r/a+b+c/new/.rss feed before the per-sub loop, so without this the CLI tests
would send real requests to Reddit (and get real 429s, since the keyless bucket
serves ~1 request per 60s window).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subscope.lib import reddit  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_prefetch: let cmd_fetch_score run the real batched prefetch "
        "instead of the no-op stub (the test must mock the transport itself)",
    )


@pytest.fixture(autouse=True)
def _packaged_config(monkeypatch):
    """Pin config resolution to the repo's packaged config/ for every test.

    cli.CONFIG_DIR resolves at import time, and it resolves to the developer's
    own ~/.config/subscope whenever that is onboarded. Without this pin the
    suite silently reads whatever profile the developer happens to have
    installed, so results differ per machine and a local fetch.yml selecting
    the search strategy sends CLI tests to the live network. Tests that want
    their own config dir still patch CONFIG_DIR themselves; this only sets the
    floor.
    """
    from subscope import cli
    monkeypatch.setattr(cli, "CONFIG_DIR", cli.PACKAGED_CONFIG_DIR)


@pytest.fixture(autouse=True)
def _no_batched_prefetch(monkeypatch, request):
    """Stub the batched multi-sub prefetch out of every test by default.

    Returning [] means "no sub was left unfetched", so the per-sub loop falls
    through to fetch_delta, which is the seam the CLI tests already mock. Tests
    that exercise the prefetch itself opt in with @pytest.mark.live_prefetch.
    """
    if "live_prefetch" in request.keywords:
        return
    monkeypatch.setattr(reddit, "prime_new_cache", lambda *a, **k: [])
