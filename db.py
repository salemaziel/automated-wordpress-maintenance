"""SQLite persistence for the web UI.

Single-process server, stdlib sqlite3, WAL mode. The DB is downstream from
the on-disk artifacts that wp_update.py owns (`logs/wp-update-*.log` and
`logs/wp-update-summary-<run_id>.json`); it ingests the summary file after a
run finishes and serves the query layer the UI needs (recent runs,
per-client history, plugin failure stats, persistent rate-limit state).

Concurrency model: one shared connection guarded by a process-wide lock.
SQLite serializes writes itself; the Python lock makes shared-cursor use
across the SSE thread, run-finalizer thread, and HTTP handlers safe.
Acceptable because peak request rate is low (single operator).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS runs (
    webui_run_id   TEXT PRIMARY KEY,
    run_id         TEXT UNIQUE,
    provider       TEXT NOT NULL,
    mode           TEXT NOT NULL,
    target         TEXT NOT NULL,
    client_file    TEXT,
    include_woo    INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    exit_code      INTEGER,
    status         TEXT NOT NULL,
    log_path       TEXT,
    summary_path   TEXT,
    started_by     TEXT,
    ingest_status  TEXT NOT NULL DEFAULT 'none',
    ingest_error   TEXT,
    ingested_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status     ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_run_id     ON runs(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_ingest     ON runs(ingest_status);

CREATE TABLE IF NOT EXISTS site_outcomes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    webui_run_id TEXT NOT NULL REFERENCES runs(webui_run_id) ON DELETE CASCADE,
    client_name  TEXT NOT NULL,
    domain       TEXT NOT NULL,
    is_staging   INTEGER NOT NULL DEFAULT 0,
    outcome      TEXT NOT NULL,
    reason       TEXT,
    finished_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_outcomes_client ON site_outcomes(client_name, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_site_outcomes_domain ON site_outcomes(domain, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_site_outcomes_run    ON site_outcomes(webui_run_id);

CREATE TABLE IF NOT EXISTS plugin_outcomes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    webui_run_id TEXT NOT NULL REFERENCES runs(webui_run_id) ON DELETE CASCADE,
    client_name  TEXT NOT NULL,
    domain       TEXT NOT NULL,
    plugin       TEXT NOT NULL,
    from_version TEXT,
    to_version   TEXT,
    status       TEXT NOT NULL,
    detail       TEXT,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plugin_outcomes_plugin ON plugin_outcomes(plugin, status);
CREATE INDEX IF NOT EXISTS idx_plugin_outcomes_domain ON plugin_outcomes(domain, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_plugin_outcomes_client ON plugin_outcomes(client_name, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_plugin_outcomes_run    ON plugin_outcomes(webui_run_id);

CREATE TABLE IF NOT EXISTS auth_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    ip        TEXT NOT NULL,
    username  TEXT,
    event     TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_events_ts ON auth_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_auth_events_ip ON auth_events(ip, ts DESC);
"""


_DB_LOCK = threading.RLock()


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open or create the DB and apply the latest schema."""
    conn = sqlite3.connect(
        str(path),
        check_same_thread=False,
        timeout=10.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    with _DB_LOCK:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        _apply_schema(conn)
    return conn


_MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_V1),
]


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Apply pending migrations as ordered scripts.

    `executescript` issues its own COMMIT before running, which conflicts
    with an outer BEGIN IMMEDIATE; instead, every migration script must
    be idempotent (CREATE TABLE/INDEX IF NOT EXISTS), and the
    schema_version row is bumped in a separate short transaction after
    the script returns. A crash between the script and the version bump
    leaves the schema fully applied but the version unbumped, and the
    next boot re-applies the same idempotent script — safe.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
    )
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = int(row[0]) if row else 0
    for version, script in _MIGRATIONS:
        if version <= current:
            continue
        conn.executescript(script)
        if current == 0:
            conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
        else:
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        current = version


@contextmanager
def _txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    with _DB_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


@contextmanager
def _read(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialize reads under the same lock so cursor state stays sane
    on the shared connection."""
    with _DB_LOCK:
        yield conn


def _now_iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def insert_run_started(
    conn: sqlite3.Connection,
    *,
    webui_run_id: str,
    provider: str,
    mode: str,
    target: str,
    client_file: str | None,
    include_woo: bool,
    started_by: str | None,
    started_at: float | None = None,
) -> None:
    iso = _now_iso(started_at) if started_at is not None else _now_iso()
    with _txn(conn):
        conn.execute(
            """
            INSERT INTO runs(
                webui_run_id, provider, mode, target, client_file,
                include_woo, started_at, status, started_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                webui_run_id, provider, mode, target, client_file,
                1 if include_woo else 0, iso, started_by,
            ),
        )


def attach_run_id(conn: sqlite3.Connection, webui_run_id: str, run_id: str) -> None:
    """Bind the wp_update.py-chosen run_id to the webui run record."""
    with _txn(conn):
        conn.execute(
            "UPDATE runs SET run_id = ? WHERE webui_run_id = ? AND run_id IS NULL",
            (run_id, webui_run_id),
        )


def update_run_finished(
    conn: sqlite3.Connection,
    *,
    webui_run_id: str,
    status: str,
    exit_code: int | None,
    log_path: str | None = None,
    summary_path: str | None = None,
    finished_at: float | None = None,
) -> None:
    """Mark a run finished AND mark ingest pending in one transaction.

    `ingest_status='pending'` lets the startup reconcile retry summaries
    that never made it into site_outcomes/plugin_outcomes.
    """
    iso = _now_iso(finished_at) if finished_at is not None else _now_iso()
    with _txn(conn):
        conn.execute(
            """
            UPDATE runs
               SET status = ?, exit_code = ?, finished_at = ?,
                   log_path = COALESCE(?, log_path),
                   summary_path = COALESCE(?, summary_path),
                   ingest_status = CASE
                       WHEN ? IS NOT NULL THEN 'pending'
                       ELSE ingest_status
                   END
             WHERE webui_run_id = ?
            """,
            (status, exit_code, iso, log_path, summary_path,
             summary_path, webui_run_id),
        )


def mark_ingest_done(conn: sqlite3.Connection, *, webui_run_id: str) -> None:
    with _txn(conn):
        conn.execute(
            """
            UPDATE runs
               SET ingest_status = 'done',
                   ingest_error  = NULL,
                   ingested_at   = ?
             WHERE webui_run_id = ?
            """,
            (_now_iso(), webui_run_id),
        )


def mark_ingest_failed(
    conn: sqlite3.Connection, *, webui_run_id: str, error: str
) -> None:
    with _txn(conn):
        conn.execute(
            """
            UPDATE runs
               SET ingest_status = 'failed',
                   ingest_error  = ?,
                   ingested_at   = ?
             WHERE webui_run_id = ?
            """,
            (error[:500], _now_iso(), webui_run_id),
        )


def ingest_run_summary(
    conn: sqlite3.Connection,
    *,
    webui_run_id: str,
    summary: dict[str, Any],
) -> None:
    """Insert site_outcomes + plugin_outcomes for a finished run.

    Idempotent: clears prior rows for this webui_run_id before inserting.
    Tolerant of partial summaries (silently skips malformed entries).
    Marks ingest_status='done' on success; raises and the caller marks
    'failed' on exception.
    """
    sites = summary.get("sites") or []
    if not isinstance(sites, list):
        with _txn(conn):
            conn.execute(
                "UPDATE runs SET ingest_status='done', ingested_at=? WHERE webui_run_id=?",
                (_now_iso(), webui_run_id),
            )
        return

    site_rows: list[tuple[Any, ...]] = []
    plugin_rows: list[tuple[Any, ...]] = []
    fallback_finished = _now_iso()

    for site in sites:
        if not isinstance(site, dict):
            continue
        client_name = str(site.get("client") or "").strip() or "unknown"
        domain = str(site.get("domain") or "").strip() or "unknown"
        is_staging = 1 if site.get("is_staging") else 0
        outcome = str(site.get("overall") or "unknown").strip() or "unknown"
        reason = (
            str(site.get("failure_detail") or site.get("failure_step") or "").strip()
            or None
        )
        finished_at = _last_step_end(site.get("steps")) or fallback_finished
        site_rows.append(
            (webui_run_id, client_name, domain, is_staging, outcome, reason, finished_at)
        )
        plugin_rows.extend(
            _extract_plugin_rows(
                webui_run_id, client_name, domain, site, fallback_finished
            )
        )

    with _txn(conn):
        conn.execute(
            "DELETE FROM site_outcomes WHERE webui_run_id = ?", (webui_run_id,)
        )
        conn.execute(
            "DELETE FROM plugin_outcomes WHERE webui_run_id = ?", (webui_run_id,)
        )
        if site_rows:
            conn.executemany(
                """
                INSERT INTO site_outcomes(
                    webui_run_id, client_name, domain, is_staging,
                    outcome, reason, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                site_rows,
            )
        if plugin_rows:
            conn.executemany(
                """
                INSERT INTO plugin_outcomes(
                    webui_run_id, client_name, domain, plugin,
                    from_version, to_version, status, detail, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                plugin_rows,
            )
        conn.execute(
            """
            UPDATE runs SET ingest_status='done', ingest_error=NULL, ingested_at=?
             WHERE webui_run_id=?
            """,
            (_now_iso(), webui_run_id),
        )


def _last_step_end(steps: Any) -> str | None:
    if not isinstance(steps, list):
        return None
    last = None
    for step in steps:
        if isinstance(step, dict) and step.get("ended"):
            last = str(step["ended"])
    return last


_PLUGIN_STATUS_MAP = {
    "success": "success",
    "updated": "success",
    "skipped": "skipped",
    "failed": "failed",
    "rolled-back-local": "rolled-back-local",
    "rolled_back_local": "rolled-back-local",
    "rolled-back": "rolled-back-local",
    "planned": "planned",
}


def _extract_plugin_rows(
    webui_run_id: str,
    client_name: str,
    domain: str,
    site: dict[str, Any],
    fallback_ts: str,
) -> Iterable[tuple[Any, ...]]:
    """Per-plugin outcome rows from site report.

    Sources (preferred first):
      1. site["steps"] entries named "plugin-update:<slug>" (canonical
         per-plugin records produced by `_step_update_plugins`).
      2. site["baseline"]["plugin_updates"] for dry-runs (no steps yet).
    """
    rows: list[tuple[Any, ...]] = []
    seen: set[str] = set()

    for step in site.get("steps") or []:
        if not isinstance(step, dict):
            continue
        name = str(step.get("name") or "")
        if not name.startswith("plugin-update:"):
            continue
        plugin = name.split(":", 1)[1].strip() or "unknown"
        status = _PLUGIN_STATUS_MAP.get(
            str(step.get("status") or "").strip().lower(), "unknown"
        )
        recorded_at = str(step.get("ended") or fallback_ts)
        detail = str(step.get("detail") or "").strip() or None
        rows.append(
            (
                webui_run_id, client_name, domain, plugin,
                None, None, status, detail, recorded_at,
            )
        )
        seen.add(plugin)

    baseline = site.get("baseline") or {}
    overall = str(site.get("overall") or "").strip().lower()
    if isinstance(baseline, dict) and overall in {"dry-run", "success"}:
        for upd in baseline.get("plugin_updates") or []:
            if not isinstance(upd, dict):
                continue
            plugin = str(upd.get("name") or "").strip()
            if not plugin or plugin in seen:
                continue
            from_v = str(upd.get("version") or "").strip() or None
            to_v = str(upd.get("update_version") or "").strip() or None
            status = "planned" if overall == "dry-run" else "success"
            rows.append(
                (
                    webui_run_id, client_name, domain, plugin,
                    from_v, to_v, status, None, fallback_ts,
                )
            )
            seen.add(plugin)

    return rows


def record_auth_event(
    conn: sqlite3.Connection,
    *,
    ip: str,
    event: str,
    username: str | None = None,
    detail: str | None = None,
) -> None:
    with _txn(conn):
        conn.execute(
            "INSERT INTO auth_events(ts, ip, username, event, detail) VALUES (?, ?, ?, ?, ?)",
            (_now_iso(), ip, username, event, detail),
        )


def check_login_rate_limit(
    conn: sqlite3.Connection,
    *,
    ip: str,
    max_failures: int = 5,
    within_seconds: int = 60,
) -> tuple[bool, int]:
    """Atomic count-and-decide for login rate limiting.

    Returns (allowed, retry_after_seconds). When blocked, also writes a
    rate_limited event row inside the same transaction so concurrent
    handlers cannot race past the threshold.
    """
    cutoff = _now_iso(time.time() - within_seconds)
    with _txn(conn):
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM auth_events
             WHERE ip = ? AND event = 'login_fail' AND ts >= ?
            """,
            (ip, cutoff),
        ).fetchone()
        n = int(row["n"]) if row else 0
        if n >= max_failures:
            conn.execute(
                """
                INSERT INTO auth_events(ts, ip, username, event, detail)
                VALUES (?, ?, NULL, 'rate_limited', ?)
                """,
                (_now_iso(), ip, f"{n} fails in {within_seconds}s"),
            )
            return False, within_seconds
    return True, 0


def recent_runs(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    client: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if client:
        # client_file is a filesystem path and unreliable for client identity;
        # join through site_outcomes which records the resolved client_name.
        where.append(
            "webui_run_id IN (SELECT webui_run_id FROM site_outcomes "
            "WHERE client_name LIKE ?)"
        )
        params.append(f"%{client}%")
    if status:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(int(limit))
    with _read(conn):
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def runs_for_client(
    conn: sqlite3.Connection, *, client_name: str, limit: int = 20
) -> list[dict[str, Any]]:
    with _read(conn):
        rows = conn.execute(
            """
            SELECT r.*
              FROM runs r
              JOIN site_outcomes s ON s.webui_run_id = r.webui_run_id
             WHERE s.client_name = ?
             GROUP BY r.webui_run_id
             ORDER BY r.started_at DESC
             LIMIT ?
            """,
            (client_name, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def client_history(
    conn: sqlite3.Connection, *, client_name: str
) -> dict[str, Any]:
    with _read(conn):
        last_touched = conn.execute(
            "SELECT MAX(finished_at) AS at FROM site_outcomes WHERE client_name = ?",
            (client_name,),
        ).fetchone()
        last_success = conn.execute(
            "SELECT MAX(finished_at) AS at FROM site_outcomes WHERE client_name = ? AND outcome = 'success'",
            (client_name,),
        ).fetchone()
        failures = conn.execute(
            """
            SELECT webui_run_id, domain, reason, finished_at
              FROM site_outcomes
             WHERE client_name = ?
               AND outcome IN ('failed', 'rolled-back')
             ORDER BY finished_at DESC
             LIMIT 10
            """,
            (client_name,),
        ).fetchall()
    return {
        "client": client_name,
        "last_touched": last_touched["at"] if last_touched else None,
        "last_success": last_success["at"] if last_success else None,
        "recent_failures": [
            {
                "run_id": row["webui_run_id"],
                "domain": row["domain"],
                "reason": row["reason"],
                "at": row["finished_at"],
            }
            for row in failures
        ],
        "recent_runs": runs_for_client(conn, client_name=client_name, limit=10),
    }


def plugin_failure_stats(
    conn: sqlite3.Connection, *, since_seconds: int = 30 * 24 * 3600, limit: int = 20
) -> list[dict[str, Any]]:
    cutoff = _now_iso(time.time() - since_seconds)
    with _read(conn):
        rows = conn.execute(
            """
            SELECT plugin,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS fail_count,
                   SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skip_count,
                   SUM(CASE WHEN status = 'rolled-back-local' THEN 1 ELSE 0 END) AS rollback_count,
                   MAX(CASE WHEN status = 'failed' THEN recorded_at END) AS last_failure_at
              FROM plugin_outcomes
             WHERE recorded_at >= ?
             GROUP BY plugin
            HAVING fail_count > 0 OR skip_count > 0 OR rollback_count > 0
             ORDER BY fail_count DESC, rollback_count DESC, skip_count DESC
             LIMIT ?
            """,
            (cutoff, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def run_summary_rows(
    conn: sqlite3.Connection, *, webui_run_id: str
) -> dict[str, Any]:
    with _read(conn):
        sites = conn.execute(
            """
            SELECT client_name, domain, is_staging, outcome, reason, finished_at
              FROM site_outcomes
             WHERE webui_run_id = ?
             ORDER BY is_staging DESC, finished_at ASC
            """,
            (webui_run_id,),
        ).fetchall()
        plugins = conn.execute(
            """
            SELECT client_name, domain, plugin, from_version, to_version,
                   status, detail, recorded_at
              FROM plugin_outcomes
             WHERE webui_run_id = ?
             ORDER BY domain, plugin
            """,
            (webui_run_id,),
        ).fetchall()
    return {
        "webui_run_id": webui_run_id,
        "sites": [dict(row) for row in sites],
        "plugins": [dict(row) for row in plugins],
    }


def get_run(
    conn: sqlite3.Connection, *, webui_run_id: str
) -> dict[str, Any] | None:
    with _read(conn):
        row = conn.execute(
            "SELECT * FROM runs WHERE webui_run_id = ?", (webui_run_id,)
        ).fetchone()
    return dict(row) if row else None


def sweep_orphan_running(
    conn: sqlite3.Connection, *, alive_ids: set[str]
) -> int:
    """Mark 'running' rows whose process is gone as 'unknown'.

    Called at server startup so a crash mid-run does not leave a row
    permanently stuck in 'running'.
    """
    with _read(conn):
        rows = conn.execute(
            "SELECT webui_run_id FROM runs WHERE status = 'running'"
        ).fetchall()
    orphans = [row["webui_run_id"] for row in rows if row["webui_run_id"] not in alive_ids]
    if not orphans:
        return 0
    placeholders = ",".join("?" for _ in orphans)
    with _txn(conn):
        conn.execute(
            f"""
            UPDATE runs
               SET status = 'unknown',
                   finished_at = COALESCE(finished_at, ?)
             WHERE webui_run_id IN ({placeholders})
            """,
            (_now_iso(), *orphans),
        )
    return len(orphans)


def pending_ingests(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Runs that finished but never had their summary ingested.

    Used by the startup reconcile to retry partial ingests without
    waiting for a manual operator action.
    """
    with _read(conn):
        rows = conn.execute(
            """
            SELECT webui_run_id, summary_path
              FROM runs
             WHERE ingest_status = 'pending'
               AND summary_path IS NOT NULL
             ORDER BY started_at ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def reconcile_pending_ingests(
    conn: sqlite3.Connection, *, log_dir: Path | None = None
) -> dict[str, int]:
    """Retry summary ingest for any 'pending' runs.

    Returns counts: {ingested, missing, parse_failed}.
    """
    counts = {"ingested": 0, "missing": 0, "parse_failed": 0}
    for row in pending_ingests(conn):
        webui_run_id = row["webui_run_id"]
        path = Path(row["summary_path"])
        if log_dir is not None and not path.is_absolute():
            path = log_dir / path
        if not path.exists():
            mark_ingest_failed(conn, webui_run_id=webui_run_id, error="summary file missing")
            counts["missing"] += 1
            continue
        summary = load_summary_file(path)
        if summary is None:
            mark_ingest_failed(
                conn, webui_run_id=webui_run_id, error="summary file unparseable"
            )
            counts["parse_failed"] += 1
            continue
        try:
            ingest_run_summary(conn, webui_run_id=webui_run_id, summary=summary)
            counts["ingested"] += 1
        except Exception as exc:  # pragma: no cover — defensive
            mark_ingest_failed(conn, webui_run_id=webui_run_id, error=str(exc))
            counts["parse_failed"] += 1
    return counts


def load_summary_file(path: Path) -> dict[str, Any] | None:
    """Best-effort parse of a wp-update-summary-<run_id>.json file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
