"""_load_yaml packaged-default fallback.

Regression for the fresh-install crash: once /subscope-onboard writes
subreddits.yml into the XDG config dir, _resolve_config_dir activates that dir,
but onboarding never writes weights.yml (the user is not asked to author it). The
loader must fall back to the packaged default in the repo `config/` dir for any
base file the synthesizer does not emit, so the first scan does not die on a
missing weights.yml. User-dir files still win when present.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subscope import cli  # noqa: E402


def test_falls_back_to_packaged_default(tmp_path, monkeypatch):
    # Simulate a freshly onboarded XDG dir: subreddits.yml present, weights.yml absent.
    (tmp_path / "subreddits.yml").write_text("subreddits: []\n")
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    weights = cli._load_yaml("weights.yml")
    assert isinstance(weights, dict) and weights  # loaded from packaged default, non-empty


def test_user_override_wins_over_packaged(tmp_path, monkeypatch):
    (tmp_path / "weights.yml").write_text("sentinel_user_override: 1\n")
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    assert cli._load_yaml("weights.yml") == {"sentinel_user_override": 1}


def test_optional_missing_in_both_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    assert cli._load_yaml("weights-nonexistent-mode.yml", optional=True) == {}


def test_required_missing_in_both_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        cli._load_yaml("definitely-not-a-real-config.yml")


def test_no_fallback_when_config_dir_is_packaged(monkeypatch):
    # When CONFIG_DIR already is the packaged dir (dev / fresh-clone), the
    # packaged file loads directly; no fallback branch, behavior unchanged.
    monkeypatch.setattr(cli, "CONFIG_DIR", cli.PACKAGED_CONFIG_DIR)
    assert cli._load_yaml("weights.yml")  # non-empty packaged default
