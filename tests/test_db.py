from __future__ import annotations

import json
from pathlib import Path

import pytest

import db


@pytest.fixture()
def conn():
    c = db.open_db(":memory:")
    yield c
    c.close()


def test_schema_applies_and_records_version(conn) -> None:
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    assert row["version"] == db.SCHEMA_VERSION
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"runs", "site_outcomes", "plugin_outcomes", "auth_events"} <= tables


def test_apply_schema_is_idempotent(conn) -> None:
    db._apply_schema(conn)
    db._apply_schema(conn)
    rows = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
    assert rows["n"] == 1


def test_insert_run_started_creates_running_row(conn) -> None:
    db.insert_run_started(
        conn,
        webui_run_id="abc123",
        provider="cloudways",
        mode="dry-run",
        target="local",
        client_file="william.json",
        include_woo=False,
        started_by="admin",
    )
    row = db.get_run(conn, webui_run_id="abc123")
    assert row is not None
    assert row["status"] == "running"
    assert row["mode"] == "dry-run"
    assert row["client_file"] == "william.json"
    assert row["ingest_status"] == "none"


def test_attach_run_id_only_sets_when_null(conn) -> None:
    db.insert_run_started(
        conn, webui_run_id="abc", provider="cloudways", mode="dry-run",
        target="local", client_file=None, include_woo=False, started_by=None,
    )
    db.attach_run_id(conn, "abc", "20260425T120000Z")
    db.attach_run_id(conn, "abc", "OTHER")  # no-op when already set
    row = db.get_run(conn, webui_run_id="abc")
    assert row["run_id"] == "20260425T120000Z"


def test_update_run_finished_marks_pending_when_summary_set(conn) -> None:
    db.insert_run_started(
        conn, webui_run_id="abc", provider="cloudways", mode="dry-run",
        target="local", client_file=None, include_woo=False, started_by=None,
    )
    db.update_run_finished(
        conn, webui_run_id="abc", status="success", exit_code=0,
        log_path="logs/wp-update-X.log",
        summary_path="logs/wp-update-summary-X.json",
    )
    row = db.get_run(conn, webui_run_id="abc")
    assert row["status"] == "success"
    assert row["exit_code"] == 0
    assert row["ingest_status"] == "pending"


def test_update_run_finished_skips_pending_when_no_summary(conn) -> None:
    db.insert_run_started(
        conn, webui_run_id="abc", provider="cloudways", mode="dry-run",
        target="local", client_file=None, include_woo=False, started_by=None,
    )
    db.update_run_finished(
        conn, webui_run_id="abc", status="cancelled", exit_code=-15,
    )
    row = db.get_run(conn, webui_run_id="abc")
    assert row["status"] == "cancelled"
    assert row["ingest_status"] == "none"


def _seed_run(conn, webui_run_id="abc"):
    db.insert_run_started(
        conn, webui_run_id=webui_run_id, provider="cloudways", mode="dry-run",
        target="local", client_file="william.json", include_woo=False,
        started_by="admin",
    )
    db.update_run_finished(
        conn, webui_run_id=webui_run_id, status="success", exit_code=0,
        summary_path=f"logs/wp-update-summary-{webui_run_id}.json",
    )


def test_ingest_run_summary_inserts_sites_and_plugins(conn) -> None:
    _seed_run(conn)
    summary = {
        "run_id": "20260425T120000Z",
        "sites": [
            {
                "client": "William",
                "domain": "wordpress-1410502-5426276.cloudwaysapps.com",
                "is_staging": True,
                "overall": "success",
                "steps": [
                    {"name": "ssh-preflight", "status": "success",
                     "ended": "2026-04-25T22:41:55+00:00", "detail": "ok"},
                    {"name": "plugin-update:perfmatters", "status": "success",
                     "ended": "2026-04-25T22:42:13+00:00",
                     "detail": "perfmatters 2.4.3->2.6.1"},
                    {"name": "plugin-update:updraftplus", "status": "skipped",
                     "ended": "2026-04-25T22:42:14+00:00",
                     "detail": "updraftplus non-fatal: license"},
                ],
                "baseline": {"plugin_updates": []},
            }
        ],
    }
    db.ingest_run_summary(conn, webui_run_id="abc", summary=summary)

    row = db.get_run(conn, webui_run_id="abc")
    assert row["ingest_status"] == "done"
    sites = conn.execute(
        "SELECT * FROM site_outcomes WHERE webui_run_id='abc'"
    ).fetchall()
    assert len(sites) == 1
    assert sites[0]["client_name"] == "William"
    assert sites[0]["outcome"] == "success"
    assert sites[0]["is_staging"] == 1
    plugins = conn.execute(
        "SELECT plugin, status, client_name, domain FROM plugin_outcomes WHERE webui_run_id='abc' ORDER BY plugin"
    ).fetchall()
    assert {(p["plugin"], p["status"]) for p in plugins} == {
        ("perfmatters", "success"),
        ("updraftplus", "skipped"),
    }
    assert all(p["client_name"] == "William" for p in plugins)


def test_ingest_dry_run_uses_baseline_for_planned_plugin_rows(conn) -> None:
    _seed_run(conn)
    summary = {
        "sites": [
            {
                "client": "William",
                "domain": "william-staging.example.com",
                "is_staging": True,
                "overall": "dry-run",
                "steps": [],
                "baseline": {
                    "plugin_updates": [
                        {"name": "perfmatters", "version": "2.4.3", "update_version": "2.6.1"},
                    ]
                },
            }
        ],
    }
    db.ingest_run_summary(conn, webui_run_id="abc", summary=summary)
    plugins = conn.execute(
        "SELECT plugin, status, from_version, to_version FROM plugin_outcomes"
    ).fetchall()
    assert len(plugins) == 1
    assert plugins[0]["status"] == "planned"
    assert plugins[0]["from_version"] == "2.4.3"
    assert plugins[0]["to_version"] == "2.6.1"


def test_ingest_is_idempotent(conn) -> None:
    _seed_run(conn)
    summary = {
        "sites": [
            {
                "client": "C", "domain": "d.example.com", "is_staging": False,
                "overall": "success",
                "steps": [
                    {"name": "plugin-update:foo", "status": "success",
                     "ended": "2026-04-25T22:42:13+00:00", "detail": ""},
                ],
                "baseline": {},
            }
        ]
    }
    db.ingest_run_summary(conn, webui_run_id="abc", summary=summary)
    db.ingest_run_summary(conn, webui_run_id="abc", summary=summary)
    n_sites = conn.execute(
        "SELECT COUNT(*) AS n FROM site_outcomes WHERE webui_run_id='abc'"
    ).fetchone()["n"]
    n_plugins = conn.execute(
        "SELECT COUNT(*) AS n FROM plugin_outcomes WHERE webui_run_id='abc'"
    ).fetchone()["n"]
    assert n_sites == 1
    assert n_plugins == 1


def test_recent_runs_filters_by_client_name_via_site_outcomes(conn) -> None:
    _seed_run(conn, "abc")
    _seed_run(conn, "def")
    db.ingest_run_summary(conn, webui_run_id="abc", summary={
        "sites": [{"client": "William", "domain": "w.example.com",
                   "overall": "success", "is_staging": False, "steps": []}]
    })
    db.ingest_run_summary(conn, webui_run_id="def", summary={
        "sites": [{"client": "Alfredo", "domain": "a.example.com",
                   "overall": "success", "is_staging": False, "steps": []}]
    })
    william = db.recent_runs(conn, client="William")
    alfredo = db.recent_runs(conn, client="Alfredo")
    assert {r["webui_run_id"] for r in william} == {"abc"}
    assert {r["webui_run_id"] for r in alfredo} == {"def"}


def test_client_history_returns_last_touched_and_failures(conn) -> None:
    _seed_run(conn, "ok")
    _seed_run(conn, "bad")
    db.ingest_run_summary(conn, webui_run_id="ok", summary={
        "sites": [{"client": "C", "domain": "d.example.com", "overall": "success",
                   "is_staging": False,
                   "steps": [{"name": "x", "status": "success",
                              "ended": "2026-04-25T10:00:00+00:00"}]}]
    })
    db.ingest_run_summary(conn, webui_run_id="bad", summary={
        "sites": [{"client": "C", "domain": "d.example.com", "overall": "failed",
                   "is_staging": False,
                   "failure_detail": "plugin X broke",
                   "steps": [{"name": "x", "status": "failed",
                              "ended": "2026-04-25T11:00:00+00:00"}]}]
    })
    h = db.client_history(conn, client_name="C")
    assert h["last_touched"] == "2026-04-25T11:00:00+00:00"
    assert h["last_success"] == "2026-04-25T10:00:00+00:00"
    assert len(h["recent_failures"]) == 1
    assert h["recent_failures"][0]["reason"] == "plugin X broke"


def test_plugin_failure_stats_aggregates_by_plugin(conn) -> None:
    _seed_run(conn, "r1")
    _seed_run(conn, "r2")
    db.ingest_run_summary(conn, webui_run_id="r1", summary={
        "sites": [{"client": "C", "domain": "d", "overall": "success",
                   "is_staging": False,
                   "steps": [
                       {"name": "plugin-update:perfmatters", "status": "failed",
                        "ended": "2026-04-25T10:00:00+00:00", "detail": "broke"},
                       {"name": "plugin-update:wpseo", "status": "skipped",
                        "ended": "2026-04-25T10:00:01+00:00", "detail": ""},
                   ],
                   "baseline": {}}]
    })
    db.ingest_run_summary(conn, webui_run_id="r2", summary={
        "sites": [{"client": "C", "domain": "d", "overall": "success",
                   "is_staging": False,
                   "steps": [
                       {"name": "plugin-update:perfmatters", "status": "failed",
                        "ended": "2026-04-25T11:00:00+00:00", "detail": "broke"},
                   ],
                   "baseline": {}}]
    })
    stats = {row["plugin"]: row for row in db.plugin_failure_stats(conn)}
    assert stats["perfmatters"]["fail_count"] == 2
    assert stats["wpseo"]["skip_count"] == 1
    assert stats["perfmatters"]["last_failure_at"] == "2026-04-25T11:00:00+00:00"


def test_check_login_rate_limit_blocks_after_threshold(conn) -> None:
    for _ in range(5):
        db.record_auth_event(conn, ip="1.2.3.4", event="login_fail",
                             username="admin")
    allowed, retry = db.check_login_rate_limit(conn, ip="1.2.3.4",
                                               max_failures=5,
                                               within_seconds=60)
    assert allowed is False
    assert retry == 60
    rl = conn.execute(
        "SELECT COUNT(*) AS n FROM auth_events WHERE event='rate_limited'"
    ).fetchone()["n"]
    assert rl == 1


def test_check_login_rate_limit_allows_when_under_threshold(conn) -> None:
    for _ in range(2):
        db.record_auth_event(conn, ip="1.2.3.4", event="login_fail",
                             username="admin")
    allowed, retry = db.check_login_rate_limit(conn, ip="1.2.3.4")
    assert allowed is True
    assert retry == 0


def test_check_login_rate_limit_isolates_per_ip(conn) -> None:
    for _ in range(5):
        db.record_auth_event(conn, ip="1.2.3.4", event="login_fail",
                             username="admin")
    allowed_other, _ = db.check_login_rate_limit(conn, ip="9.9.9.9")
    assert allowed_other is True


def test_sweep_orphan_running_marks_dead_runs_unknown(conn) -> None:
    db.insert_run_started(
        conn, webui_run_id="alive", provider="cloudways", mode="dry-run",
        target="local", client_file=None, include_woo=False, started_by=None,
    )
    db.insert_run_started(
        conn, webui_run_id="dead", provider="cloudways", mode="dry-run",
        target="local", client_file=None, include_woo=False, started_by=None,
    )
    n = db.sweep_orphan_running(conn, alive_ids={"alive"})
    assert n == 1
    assert db.get_run(conn, webui_run_id="dead")["status"] == "unknown"
    assert db.get_run(conn, webui_run_id="alive")["status"] == "running"


def test_reconcile_pending_ingests_picks_up_summary_file(tmp_path: Path, conn) -> None:
    _seed_run(conn, "abc")
    summary = {
        "sites": [{"client": "C", "domain": "d", "overall": "success",
                   "is_staging": False,
                   "steps": [{"name": "plugin-update:foo", "status": "success",
                              "ended": "2026-04-25T10:00:00+00:00"}]}]
    }
    summary_path = tmp_path / "wp-update-summary-abc.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    conn.execute(
        "UPDATE runs SET summary_path = ? WHERE webui_run_id = ?",
        (str(summary_path), "abc"),
    )
    counts = db.reconcile_pending_ingests(conn)
    assert counts == {"ingested": 1, "missing": 0, "parse_failed": 0}
    assert db.get_run(conn, webui_run_id="abc")["ingest_status"] == "done"


def test_reconcile_pending_ingests_marks_missing_failed(tmp_path: Path, conn) -> None:
    _seed_run(conn, "abc")
    conn.execute(
        "UPDATE runs SET summary_path = ? WHERE webui_run_id = ?",
        (str(tmp_path / "does-not-exist.json"), "abc"),
    )
    counts = db.reconcile_pending_ingests(conn)
    assert counts == {"ingested": 0, "missing": 1, "parse_failed": 0}
    row = db.get_run(conn, webui_run_id="abc")
    assert row["ingest_status"] == "failed"
    assert "missing" in (row["ingest_error"] or "")


def test_real_summary_fixture_round_trips(conn) -> None:
    fixture = (
        Path(__file__).resolve().parent.parent
        / "logs"
        / "wp-update-summary-20260425T224153Z.json"
    )
    if not fixture.exists():
        pytest.skip("real summary fixture not present")
    summary = json.loads(fixture.read_text(encoding="utf-8"))
    _seed_run(conn, "real")
    db.ingest_run_summary(conn, webui_run_id="real", summary=summary)
    sites = conn.execute(
        "SELECT outcome FROM site_outcomes WHERE webui_run_id='real'"
    ).fetchall()
    assert len(sites) == 1
    assert sites[0]["outcome"] == "dry-run"
    plugins = conn.execute(
        "SELECT plugin, status FROM plugin_outcomes WHERE webui_run_id='real'"
    ).fetchall()
    assert any(p["plugin"] == "perfmatters" and p["status"] == "planned" for p in plugins)
