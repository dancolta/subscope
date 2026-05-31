"""cli._build_mode_query: deterministic, bounded within-sub search query builder."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subscope import cli  # noqa: E402
from subscope.lib import reddit  # noqa: E402


def test_multiword_terms_quoted():
    q = cli._build_mode_query("churn", ["price hike", "renewal"], [])
    assert '"price hike"' in q
    assert "renewal" in q
    assert " OR " in q


def test_rivals_leads_with_brand_anchor():
    q = cli._build_mode_query("rivals", ["switching", "cancel"], ["ConnectWise", "Datto"])
    assert "ConnectWise" in q and "Datto" in q
    # rivals is competitor-mention intel: brands ARE the query, not the keywords.
    assert "switching" not in q and "cancel" not in q


def test_rivals_falls_back_to_keywords_without_brands():
    q = cli._build_mode_query("rivals", ["switching", "cancel"], [])
    assert "switching" in q or "cancel" in q


def test_non_rivals_appends_two_brands():
    q = cli._build_mode_query("churn", ["canceling"], ["HubSpot", "Salesforce", "Pipedrive"])
    assert "HubSpot" in q and "Salesforce" in q
    assert "Pipedrive" not in q  # capped at 2 brand anchors for non-rivals


def test_deterministic_order_independent():
    a = cli._build_mode_query("churn", ["zebra", "alpha", "mango"], [])
    b = cli._build_mode_query("churn", ["mango", "zebra", "alpha"], [])
    assert a == b  # sorted, so set-ordering of the bucket cannot drift the query


def test_query_bounded_under_max_len():
    huge = [f"longkeywordnumber{i:04d}" for i in range(500)]
    q = cli._build_mode_query("churn", huge, [])
    assert len(q) <= reddit.QUERY_MAX_LEN


def test_empty_bucket_no_crash():
    assert cli._build_mode_query("churn", [], []) == ""


def test_dedup_case_insensitive():
    q = cli._build_mode_query("churn", ["Renewal", "renewal"], [])
    assert q.lower().count("renewal") == 1
