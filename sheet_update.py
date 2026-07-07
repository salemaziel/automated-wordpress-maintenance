"""Update the Plugin Updates Google Sheet after a successful execute run.

End-of-run hook for wp_update.py: matches sites that successfully updated
against rows in the sheet (column E is the wp-admin login URL), then writes
the next Monday's date to column B (Next Update) and today's date to column
C (Last Updated). Failed/skipped/rolled-back sites are never touched.

The match is keyed on bare domain — column E's URL has its protocol, www
prefix, and any path (including /wp-admin) stripped before comparison.
Rows whose URL doesn't reduce to a domain matching one of our SiteReports
(e.g. custom login pages on sites we don't maintain) are silently skipped.

Shells out to `gws sheets spreadsheets values get|batchUpdate`. The two
shell-out helpers are split out so tests can mock them without going to the
network.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

_log = logging.getLogger(__name__)


# A1 column letters for the two cells we update per matching row.
NEXT_UPDATE_COL = "B"
LAST_UPDATED_COL = "C"
URL_COL = "E"
HEADER_ROW = 1  # data starts at row 2


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_PROTO_RE = re.compile(r"^https?://", re.IGNORECASE)


def normalize_admin_url(url: str | None) -> str:
    """Reduce a column-E URL (or any URL) to a bare lowercase domain.

    Examples:
        https://btss.co/wp-admin/      -> btss.co
        https://www.example.com/x/y    -> example.com
        http://Foo.COM                 -> foo.com
        '  https://x.com/wp-admin '    -> x.com
        ''                             -> ''
    """
    if not url:
        return ""
    s = url.strip().lower()
    s = _PROTO_RE.sub("", s)
    # Cut at the first path separator — we only care about the host.
    s = s.split("/", 1)[0]
    s = s.split("?", 1)[0]
    s = s.split("#", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s.strip()


def next_monday(today: date) -> date:
    """Return the next Monday strictly after `today`. If today is Monday,
    returns today + 7 days (i.e. one week out)."""
    days = (7 - today.weekday()) % 7 or 7
    return today + timedelta(days=days)


_MONTH_ABBR = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_sheet_date(d: date) -> str:
    """Render a date in the sheet's existing format, e.g. '8 May 2026'."""
    # Locale-independent English abbreviation. strftime('%b') would emit a
    # localized month name (e.g. 'Mai') on a non-English host and corrupt
    # the sheet; a static lookup keeps output stable regardless of locale.
    return f"{d.day} {_MONTH_ABBR[d.month]} {d.year}"


def build_value_ranges(
    row_to_dates: dict[int, tuple[str, str]],
    tab_name: str,
) -> list[dict]:
    """Build the `data` list for a Sheets batchUpdate call.

    `row_to_dates` maps a 1-indexed row number to (next_update, last_updated)
    strings. Each row becomes one ValueRange covering B<row>:C<row>.
    """
    tab = _quote_tab(tab_name)
    out: list[dict] = []
    for row in sorted(row_to_dates):
        nxt, last = row_to_dates[row]
        out.append({
            "range": f"{tab}!{NEXT_UPDATE_COL}{row}:{LAST_UPDATED_COL}{row}",
            "values": [[nxt, last]],
        })
    return out


def _quote_tab(tab_name: str) -> str:
    """Single-quote a tab name for A1 notation if it contains spaces or
    other non-bareword chars. Embedded single quotes are doubled."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tab_name):
        return tab_name
    escaped = tab_name.replace("'", "''")
    return f"'{escaped}'"


# ---------------------------------------------------------------------------
# Shell-out wrappers (mocked in tests)
# ---------------------------------------------------------------------------

def fetch_admin_urls(
    spreadsheet_id: str,
    tab_name: str,
    *,
    gws_path: str = "gws",
    timeout: int = 30,
) -> dict[str, int]:
    """Return `{normalized_domain: row_index}` for every non-empty row in
    column E. Raises subprocess.CalledProcessError on shell-out failure."""
    params = json.dumps({
        "spreadsheetId": spreadsheet_id,
        "range": f"{_quote_tab(tab_name)}!{URL_COL}{HEADER_ROW + 1}:{URL_COL}",
        "majorDimension": "ROWS",
    })
    cmd = [
        gws_path, "sheets", "spreadsheets", "values", "get",
        "--params", params, "--format", "json",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=True,
    )
    payload = json.loads(proc.stdout or "{}")
    rows = payload.get("values") or []
    out: dict[str, int] = {}
    for offset, row in enumerate(rows):
        cell = row[0] if row else ""
        domain = normalize_admin_url(cell)
        if not domain or domain in out:
            continue
        out[domain] = offset + HEADER_ROW + 1  # 1-indexed sheet row
    return out


def post_sheet_updates(
    spreadsheet_id: str,
    value_ranges: list[dict],
    *,
    gws_path: str = "gws",
    timeout: int = 30,
) -> dict:
    """Send a `values.batchUpdate` request. Returns the parsed response.
    Raises subprocess.CalledProcessError on shell-out failure."""
    body = {
        "valueInputOption": "USER_ENTERED",
        "data": value_ranges,
    }
    params = json.dumps({"spreadsheetId": spreadsheet_id})
    cmd = [
        gws_path, "sheets", "spreadsheets", "values", "batchUpdate",
        "--params", params, "--json", json.dumps(body), "--format", "json",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=True,
    )
    return json.loads(proc.stdout or "{}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

@dataclass
class SheetUpdateResult:
    matched: list[tuple[str, int]] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    updated_cells: int = 0
    dry_run: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def update_sheet_for_successes(
    *,
    spreadsheet_id: str,
    tab_name: str,
    success_domains: Iterable[str],
    today: date,
    gws_path: str = "gws",
    dry_run: bool = False,
    log: logging.Logger | None = None,
) -> SheetUpdateResult:
    """Match `success_domains` (SiteReport.domain values) against the sheet's
    column-E URLs and write next-Monday/today into B/C for each match.

    `dry_run=True` skips the batchUpdate call and just reports what would
    have been written. Errors are captured on the result rather than raised
    so an end-of-run hook never crashes the whole run."""
    logger = log or _log
    result = SheetUpdateResult(dry_run=dry_run)

    domains = sorted({normalize_admin_url(d) for d in success_domains if d})
    domains = [d for d in domains if d]
    if not domains:
        logger.info("sheet-update: no successful sites to report — skipping")
        return result

    try:
        url_to_row = fetch_admin_urls(
            spreadsheet_id, tab_name, gws_path=gws_path,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        result.error = f"fetch failed: {stderr or exc}"
        logger.warning("sheet-update: %s", result.error)
        return result
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        result.error = f"fetch failed: {exc}"
        logger.warning("sheet-update: %s", result.error)
        return result

    next_str = format_sheet_date(next_monday(today))
    today_str = format_sheet_date(today)
    row_to_dates: dict[int, tuple[str, str]] = {}
    for domain in domains:
        row = url_to_row.get(domain)
        if row is None:
            result.unmatched.append(domain)
            continue
        result.matched.append((domain, row))
        row_to_dates[row] = (next_str, today_str)

    if not row_to_dates:
        logger.info(
            "sheet-update: %d successful site(s) but none matched the sheet "
            "(unmatched: %s)",
            len(domains), ", ".join(result.unmatched) or "none",
        )
        return result

    value_ranges = build_value_ranges(row_to_dates, tab_name)

    if dry_run:
        logger.info(
            "sheet-update [DRY-RUN]: would write %d row(s): %s",
            len(value_ranges),
            ", ".join(f"{d}@row{r}" for d, r in result.matched),
        )
        result.updated_cells = len(value_ranges) * 2
        return result

    try:
        resp = post_sheet_updates(
            spreadsheet_id, value_ranges, gws_path=gws_path,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        result.error = f"batchUpdate failed: {stderr or exc}"
        logger.warning("sheet-update: %s", result.error)
        return result
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        result.error = f"batchUpdate failed: {exc}"
        logger.warning("sheet-update: %s", result.error)
        return result

    result.updated_cells = int(resp.get("totalUpdatedCells") or 0)
    logger.info(
        "sheet-update: wrote %d cell(s) across %d row(s): %s",
        result.updated_cells, len(value_ranges),
        ", ".join(f"{d}@row{r}" for d, r in result.matched),
    )
    if result.unmatched:
        logger.info(
            "sheet-update: %d successful site(s) had no sheet row: %s",
            len(result.unmatched), ", ".join(result.unmatched),
        )
    return result
