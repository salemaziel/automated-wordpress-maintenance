from __future__ import annotations

import json
import subprocess
from datetime import date
from unittest.mock import patch

import pytest

import sheet_update as su

# ---------------------------------------------------------------------------
# normalize_admin_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("https://btss.co/wp-admin/", "btss.co"),
    ("https://birchall-restoration.com/wp-admin", "birchall-restoration.com"),
    ("https://centerseptic.com/wp-admin ", "centerseptic.com"),
    ("  https://x.com/wp-admin ", "x.com"),
    ("https://Angelscapes.NET/wp-admin/", "angelscapes.net"),
    ("https://www.example.com/wp-admin/", "example.com"),
    ("http://foo.com", "foo.com"),
    ("https://characterandleadership.com/cdl-login", "characterandleadership.com"),
    ("foo.com", "foo.com"),
    ("", ""),
    (None, ""),
    ("   ", ""),
    ("https://x.com/wp-admin/?x=1", "x.com"),
    ("https://x.com#frag", "x.com"),
])
def test_normalize_admin_url(raw, expected):
    assert su.normalize_admin_url(raw) == expected


# ---------------------------------------------------------------------------
# next_monday — covers every weekday including the Monday-rolls-forward case
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("today_iso,expected_iso", [
    ("2026-05-11", "2026-05-18"),  # Monday    -> +7
    ("2026-05-12", "2026-05-18"),  # Tuesday   -> +6
    ("2026-05-13", "2026-05-18"),  # Wednesday -> +5
    ("2026-05-14", "2026-05-18"),  # Thursday  -> +4
    ("2026-05-15", "2026-05-18"),  # Friday    -> +3
    ("2026-05-16", "2026-05-18"),  # Saturday  -> +2
    ("2026-05-17", "2026-05-18"),  # Sunday    -> +1
])
def test_next_monday_strict(today_iso, expected_iso):
    assert su.next_monday(date.fromisoformat(today_iso)) == date.fromisoformat(expected_iso)


def test_format_sheet_date_no_leading_zero_on_day():
    assert su.format_sheet_date(date(2026, 5, 8)) == "8 May 2026"
    assert su.format_sheet_date(date(2026, 5, 18)) == "18 May 2026"
    assert su.format_sheet_date(date(2026, 1, 1)) == "1 Jan 2026"


# ---------------------------------------------------------------------------
# build_value_ranges + tab name quoting
# ---------------------------------------------------------------------------

def test_build_value_ranges_quotes_tab_with_spaces():
    out = su.build_value_ranges(
        {5: ("18 May 2026", "12 May 2026"), 2: ("18 May 2026", "12 May 2026")},
        tab_name="Plugin Updates",
    )
    # Sorted by row ascending.
    assert [r["range"] for r in out] == [
        "'Plugin Updates'!B2:C2",
        "'Plugin Updates'!B5:C5",
    ]
    assert out[0]["values"] == [["18 May 2026", "12 May 2026"]]


def test_build_value_ranges_bareword_tab_unquoted():
    out = su.build_value_ranges({2: ("a", "b")}, tab_name="Sheet1")
    assert out[0]["range"] == "Sheet1!B2:C2"


def test_quote_tab_escapes_embedded_single_quotes():
    assert su._quote_tab("Bob's Sheet") == "'Bob''s Sheet'"


# ---------------------------------------------------------------------------
# fetch_admin_urls — covers row-index math, dedupe, and empty-cell skipping
# ---------------------------------------------------------------------------

def _mock_subproc_run(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    """Patch subprocess.run inside sheet_update with a canned response."""
    def _run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=stderr,
        )
    return patch("sheet_update.subprocess.run", side_effect=_run)


def test_fetch_admin_urls_indexes_from_row_two_and_skips_empty():
    payload = {
        "range": "Plugin Updates!E2:E",
        "majorDimension": "ROWS",
        "values": [
            ["https://btss.co/wp-admin/"],            # row 2
            [],                                       # row 3 — empty
            ["https://centerseptic.com/wp-admin "],   # row 4
            ["https://www.angelscapes.net/wp-admin"], # row 5
            ["https://btss.co/wp-admin"],             # row 6 — dup, first wins
        ],
    }
    with _mock_subproc_run(stdout=json.dumps(payload)):
        urls = su.fetch_admin_urls("SHEET_ID", "Plugin Updates")
    assert urls == {
        "btss.co": 2,
        "centerseptic.com": 4,
        "angelscapes.net": 5,
    }


def test_fetch_admin_urls_returns_empty_when_sheet_has_no_data():
    with _mock_subproc_run(stdout=json.dumps({"range": "x", "majorDimension": "ROWS"})):
        assert su.fetch_admin_urls("SHEET_ID", "Plugin Updates") == {}


# ---------------------------------------------------------------------------
# update_sheet_for_successes — the end-to-end orchestrator
# ---------------------------------------------------------------------------

def _patch_fetch(url_to_row):
    return patch("sheet_update.fetch_admin_urls", return_value=url_to_row)


def _patch_post(resp=None):
    return patch("sheet_update.post_sheet_updates", return_value=resp or {"totalUpdatedCells": 4})


def test_orchestrator_writes_for_matched_sites():
    today = date(2026, 5, 12)  # Tuesday → next Monday 2026-05-18
    fetch = _patch_fetch({
        "btss.co": 5,
        "centerseptic.com": 10,
        "angelscapes.net": 2,
    })
    with fetch, _patch_post({"totalUpdatedCells": 4}) as post_mock:
        res = su.update_sheet_for_successes(
            spreadsheet_id="SHEET_ID",
            tab_name="Plugin Updates",
            success_domains=["btss.co", "centerseptic.com"],
            today=today,
        )
    assert res.ok
    assert res.dry_run is False
    assert sorted(res.matched) == [("btss.co", 5), ("centerseptic.com", 10)]
    assert res.unmatched == []
    assert res.updated_cells == 4
    # Confirm the request body had the correct dates + ranges.
    posted_value_ranges = post_mock.call_args.args[1]
    assert sorted(r["range"] for r in posted_value_ranges) == [
        "'Plugin Updates'!B10:C10",
        "'Plugin Updates'!B5:C5",
    ]
    assert posted_value_ranges[0]["values"][0] == ["18 May 2026", "12 May 2026"]


def test_orchestrator_records_unmatched_and_does_not_post():
    fetch = _patch_fetch({"btss.co": 5})
    with fetch, _patch_post() as post_mock:
        res = su.update_sheet_for_successes(
            spreadsheet_id="SHEET_ID",
            tab_name="Plugin Updates",
            success_domains=["someone-else.com"],
            today=date(2026, 5, 12),
        )
    assert res.ok
    assert res.matched == []
    assert res.unmatched == ["someone-else.com"]
    assert post_mock.call_count == 0
    assert res.updated_cells == 0


def test_orchestrator_dry_run_does_not_post():
    fetch = _patch_fetch({"btss.co": 5})
    with fetch, _patch_post() as post_mock:
        res = su.update_sheet_for_successes(
            spreadsheet_id="SHEET_ID",
            tab_name="Plugin Updates",
            success_domains=["btss.co"],
            today=date(2026, 5, 12),
            dry_run=True,
        )
    assert res.ok
    assert res.dry_run is True
    assert res.matched == [("btss.co", 5)]
    assert res.updated_cells == 2  # 1 row × 2 cells
    assert post_mock.call_count == 0


def test_orchestrator_handles_fetch_failure_gracefully():
    err = subprocess.CalledProcessError(
        returncode=2, cmd=["gws"], output="", stderr="auth error",
    )
    with patch("sheet_update.fetch_admin_urls", side_effect=err) as fetch_mock, \
            _patch_post() as post_mock:
        res = su.update_sheet_for_successes(
            spreadsheet_id="SHEET_ID",
            tab_name="Plugin Updates",
            success_domains=["btss.co"],
            today=date(2026, 5, 12),
        )
    assert not res.ok
    assert "auth error" in res.error
    assert fetch_mock.call_count == 1
    assert post_mock.call_count == 0


def test_orchestrator_handles_post_failure_gracefully():
    err = subprocess.CalledProcessError(
        returncode=2, cmd=["gws"], output="", stderr="quota exceeded",
    )
    with _patch_fetch({"btss.co": 5}), \
            patch("sheet_update.post_sheet_updates", side_effect=err):
        res = su.update_sheet_for_successes(
            spreadsheet_id="SHEET_ID",
            tab_name="Plugin Updates",
            success_domains=["btss.co"],
            today=date(2026, 5, 12),
        )
    assert not res.ok
    assert "quota exceeded" in res.error


def test_orchestrator_normalizes_success_domains_before_matching():
    # SiteReport.domain values can carry www. or upper case — orchestrator
    # should normalize them the same way it normalizes the sheet's URLs.
    fetch = _patch_fetch({"foo.com": 3})
    with fetch, _patch_post():
        res = su.update_sheet_for_successes(
            spreadsheet_id="SHEET_ID",
            tab_name="Plugin Updates",
            success_domains=["WWW.Foo.com", "https://foo.com/wp-admin"],
            today=date(2026, 5, 12),
        )
    assert res.matched == [("foo.com", 3)]


def test_orchestrator_no_op_when_no_successes():
    with _patch_fetch({}) as fetch_mock, _patch_post() as post_mock:
        res = su.update_sheet_for_successes(
            spreadsheet_id="SHEET_ID",
            tab_name="Plugin Updates",
            success_domains=[],
            today=date(2026, 5, 12),
        )
    assert res.ok
    assert res.matched == []
    assert fetch_mock.call_count == 0
    assert post_mock.call_count == 0
