from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import webui


def valid_client() -> dict[str, object]:
    return {
        "client_name": "Example Client",
        "email": "owner@example.com",
        "server_ip_address": "203.0.113.10",
        "master_credentials": {"username": "master_x", "password": "secret"},
        "applications": [
            {
                "website_domain": "example.com",
                "path_to_public_html": "/home/master/applications/abcd1234/public_html",
                "sftp_credentials": {
                    "username": "$SSH_USER",
                    "password": "$APP_PW",
                    "ssh_key": "$SSH_KEY",
                },
                "environment_flags": {
                    "wp_cli_installed": True,
                    "is_staging": False,
                    "has_woocommerce": False,
                },
            }
        ],
    }


def test_session_signing_round_trip_and_tamper_rejection() -> None:
    token = webui.sign_session("admin", "secret", now=100)

    assert webui.verify_session(token, "secret", now=110) == "admin"
    assert webui.verify_session(token + "x", "secret", now=110) is None
    assert webui.verify_session(token, "wrong", now=110) is None
    assert webui.verify_session(token, "secret", now=100 + webui.SESSION_TTL_SECONDS + 1) is None


def test_write_client_doc_validates_and_uses_cloudways_suffix(tmp_path: Path) -> None:
    path = webui.write_client_doc(valid_client(), tmp_path)

    assert path.name == "example-client_cloudways.json"
    assert json.loads(path.read_text())["client_name"] == "Example Client"

    second = webui.write_client_doc(valid_client(), tmp_path)
    assert second.name == "example-client-2_cloudways.json"


def test_write_client_doc_rejects_missing_required_fields(tmp_path: Path) -> None:
    doc = valid_client()
    doc["server_ip_address"] = ""

    with pytest.raises(ValueError, match="server_ip_address is required"):
        webui.write_client_doc(doc, tmp_path)


def test_non_cloudways_client_allows_provider_specific_path(tmp_path: Path) -> None:
    doc = valid_client()
    doc["hosting_provider"] = "Siteground"
    doc["applications"][0]["path_to_public_html"] = "/home/customer/www/example.com/public_html"

    path = webui.write_client_doc(doc, tmp_path)

    assert path.name == "example-client_siteground.json"
    assert json.loads(path.read_text())["hosting_provider"] == "Siteground"


def test_list_client_files_filters_by_provider(tmp_path: Path) -> None:
    cloudways = valid_client()
    siteground = valid_client()
    siteground["client_name"] = "Siteground Client"
    siteground["hosting_provider"] = "Siteground"
    siteground["applications"][0]["path_to_public_html"] = "/home/customer/www/example.com/public_html"
    pressable = valid_client()
    pressable["client_name"] = "Pressable Client"
    pressable["hosting_provider"] = "Pressable"
    pressable["applications"][0]["path_to_public_html"] = "/htdocs"

    webui.write_client_doc(cloudways, tmp_path)
    webui.write_client_doc(siteground, tmp_path)
    webui.write_client_doc(pressable, tmp_path)

    assert [row["label"] for row in webui.list_client_files(tmp_path, provider="Cloudways")] == ["Example Client"]
    assert [row["label"] for row in webui.list_client_files(tmp_path, provider="Pressable")] == ["Pressable Client"]


def test_list_client_files_infers_provider_from_filename(tmp_path: Path) -> None:
    (tmp_path / "legacy_cloudways.json").write_text(json.dumps({"client_name": "Legacy Cloudways"}))
    (tmp_path / "press_pressable.json").write_text(json.dumps({"client_name": "Pressable"}))

    assert [row["label"] for row in webui.list_client_files(tmp_path, provider="Cloudways")] == ["Legacy Cloudways"]
    assert [row["label"] for row in webui.list_client_files(tmp_path, provider="Pressable")] == ["Pressable"]


def test_write_ssh_key_stores_private_key_with_restrictive_permissions(tmp_path: Path) -> None:
    key = webui.write_ssh_key(
        "cloudways key",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n",
        tmp_path,
    )

    assert key.name == "cloudways_key"
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    assert webui.list_ssh_keys(tmp_path) == [{"name": "cloudways_key", "path": str(key)}]


def test_write_ssh_key_rejects_non_private_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="private SSH key"):
        webui.write_ssh_key("public.pub", "ssh-ed25519 AAAA", tmp_path)


def test_manual_payload_to_client_uses_env_placeholders() -> None:
    doc = webui.manual_payload_to_client(
        {
            "clientName": "Manual Client",
            "email": "owner@example.com",
            "serverIp": "203.0.113.20",
            "masterUsername": "master_y",
            "masterPassword": "secret",
            "websiteDomain": "manual.example",
            "publicHtmlPath": "/home/master/applications/efgh5678/public_html",
            "provider": "Cloudron",
            "isStaging": True,
            "hasWooCommerce": True,
        }
    )

    app = doc["applications"][0]
    assert doc["hosting_provider"] == "Cloudron"
    assert app["sftp_credentials"]["username"] == "$SSH_USER"
    assert app["environment_flags"]["is_staging"] is True
    assert app["environment_flags"]["has_woocommerce"] is True


def test_local_command_defaults_to_dry_run_and_stream() -> None:
    command = webui.build_local_command({"clientFile": "example-client_cloudways.json"})

    assert command[:2] == [sys.executable, str(webui.SCRIPT_PATH)]
    assert "--stream" in command
    assert "--execute" not in command
    assert command[-2:] == ["--client-file", str(webui.CLIENTS_DIR / "example-client_cloudways.json")]


def test_local_command_can_use_uploaded_ssh_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = webui.write_ssh_key(
        "cloudways",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n",
        tmp_path,
    )
    monkeypatch.setattr(webui, "KEYS_DIR", tmp_path)

    command = webui.build_local_command({"sshKey": key.name})

    assert command[-2:] == ["--ssh-key", str(key)]


def test_local_command_adds_execute_and_woocommerce_flags() -> None:
    command = webui.build_local_command({"execute": True, "includeWooCommerce": True})

    assert "--execute" in command
    assert "--include-woocommerce" in command


def test_remote_command_wraps_streaming_script_with_ssh() -> None:
    settings = webui.Settings(
        password="pw",
        secret="secret",
        remote_host="203.0.113.30",
        remote_user="deploy",
        remote_repo_path="/srv/maintenance",
        remote_identity_file="/home/me/.ssh/id_ed25519",
    )

    command = webui.build_remote_command({"execute": True}, settings)

    assert command[:7] == ["ssh", "-p", "22", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    assert "-i" in command
    assert "deploy@203.0.113.30" in command
    assert command[-1] == "cd /srv/maintenance && exec python3 wp_update.py --stream --execute"


def test_remote_command_uses_repo_relative_client_file() -> None:
    settings = webui.Settings(
        password="pw",
        secret="secret",
        remote_host="203.0.113.30",
        remote_user="deploy",
        remote_repo_path="/srv/maintenance",
    )

    command = webui.build_remote_command(
        {"clientFile": "../example-client_cloudways.json"},
        settings,
    )

    assert command[-1].endswith(
        "wp_update.py --stream --client-file clients/example-client_cloudways.json"
    )


def test_start_run_rejects_provider_without_runner() -> None:
    with pytest.raises(ValueError, match="Siteground does not have a runner configured yet"):
        webui.start_run(
            {"provider": "Siteground"},
            webui.Settings(password="pw", secret="secret"),
        )


@pytest.fixture()
def memory_db(monkeypatch: pytest.MonkeyPatch):
    """Swap the module-level DB connection for an in-memory one for the
    duration of the test, then close + restore."""
    import db as db_module
    conn = db_module.open_db(":memory:")
    monkeypatch.setattr(webui, "DB_CONN", conn)
    yield conn
    conn.close()


def test_scan_run_metadata_extracts_run_id_and_summary(memory_db) -> None:
    import db as db_module
    db_module.insert_run_started(
        memory_db, webui_run_id="abc", provider="cloudways", mode="dry-run",
        target="local", client_file=None, include_woo=False, started_by="admin",
    )
    record = webui.RunRecord(run_id="abc", command=[], mode="dry-run", target="local")

    webui._scan_run_metadata(record, "WordPress Maintenance Run  |  ID: 20260425T120000Z")
    webui._scan_run_metadata(record, "Summary written to /home/master/logs/wp-update-summary-20260425T120000Z.json")

    assert record.wp_run_id == "20260425T120000Z"
    assert record.summary_path.endswith("wp-update-summary-20260425T120000Z.json")
    assert db_module.get_run(memory_db, webui_run_id="abc")["run_id"] == "20260425T120000Z"


def test_runs_payload_merges_live_and_finished(memory_db) -> None:
    import db as db_module
    db_module.insert_run_started(
        memory_db, webui_run_id="finished", provider="cloudways", mode="dry-run",
        target="local", client_file="william.json", include_woo=False, started_by="admin",
    )
    db_module.update_run_finished(
        memory_db, webui_run_id="finished", status="success", exit_code=0,
    )
    live = webui.RunRecord(run_id="live123", command=[], mode="dry-run", target="local")
    with webui.RUNS_LOCK:
        webui.RUNS["live123"] = live
    try:
        rows = webui.runs_payload(50, None, None)
        ids = [row["id"] for row in rows]
        assert "finished" in ids and "live123" in ids
        live_row = next(row for row in rows if row["id"] == "live123")
        assert live_row["ingest_status"] == "live"
    finally:
        with webui.RUNS_LOCK:
            webui.RUNS.pop("live123", None)


def test_run_summary_payload_returns_none_for_unknown(memory_db) -> None:
    assert webui.run_summary_payload("does-not-exist") is None


def test_client_history_payload_pulls_from_db(memory_db) -> None:
    import db as db_module
    db_module.insert_run_started(
        memory_db, webui_run_id="r1", provider="cloudways", mode="dry-run",
        target="local", client_file=None, include_woo=False, started_by=None,
    )
    db_module.update_run_finished(
        memory_db, webui_run_id="r1", status="success", exit_code=0,
        summary_path="x.json",
    )
    db_module.ingest_run_summary(memory_db, webui_run_id="r1", summary={
        "sites": [{"client": "William", "domain": "w.example.com",
                   "overall": "success", "is_staging": False,
                   "steps": [{"name": "x", "status": "success",
                              "ended": "2026-04-25T10:00:00+00:00"}]}]
    })
    payload = webui.client_history_payload("William")
    assert payload["last_touched"] == "2026-04-25T10:00:00+00:00"
    assert payload["recent_runs"][0]["id"] == "r1"


def test_session_carries_csrf_token() -> None:
    token = webui.sign_session("admin", "secret", now=100, csrf="abc123")
    assert webui.verify_session(token, "secret", now=110) == "admin"
    assert webui.session_csrf(token, "secret", now=110) == "abc123"
    assert webui.session_csrf(token, "secret", now=100 + webui.SESSION_TTL_SECONDS + 1) is None


def test_session_without_csrf_returns_none() -> None:
    token = webui.sign_session("admin", "secret", now=100)
    assert webui.session_csrf(token, "secret", now=110) is None


def test_mark_finished_honors_cancel_request() -> None:
    record = webui.RunRecord(run_id="x", command=[], mode="dry-run", target="local")
    record.cancel_requested = True
    record.mark_finished(-15)
    assert record.status == "cancelled"
    assert record.exit_code == -15


def test_cancel_run_returns_unknown_for_missing() -> None:
    assert webui.cancel_run("does-not-exist")["status"] == "unknown"


def test_cancel_run_is_idempotent_for_already_finished() -> None:
    record = webui.RunRecord(run_id="done1", command=[], mode="dry-run", target="local")
    record.mark_finished(0)
    with webui.RUNS_LOCK:
        webui.RUNS["done1"] = record
    try:
        result = webui.cancel_run("done1")
        assert result == {"id": "done1", "status": "success"}
    finally:
        with webui.RUNS_LOCK:
            webui.RUNS.pop("done1", None)


def test_cancel_run_signals_live_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, object] = {}

    class FakeProc:
        pid = 999

        def poll(self) -> None:  # still running
            return None

    record = webui.RunRecord(run_id="live1", command=[], mode="dry-run", target="local")
    record.proc = FakeProc()  # type: ignore[assignment]

    def fake_killpg(pid: int, sig: int) -> None:
        sent["pid"] = pid
        sent["sig"] = sig

    monkeypatch.setattr(webui.os, "killpg", fake_killpg)

    with webui.RUNS_LOCK:
        webui.RUNS["live1"] = record
    try:
        result = webui.cancel_run("live1", grace=0.05)
        assert result == {"id": "live1", "status": "cancelling"}
        assert sent["pid"] == 999
        assert record.cancel_requested is True
    finally:
        with webui.RUNS_LOCK:
            webui.RUNS.pop("live1", None)


def test_finalize_skips_summary_ingest_for_remote_target(memory_db, tmp_path: Path) -> None:
    import db as db_module
    db_module.insert_run_started(
        memory_db, webui_run_id="rem1", provider="cloudways", mode="dry-run",
        target="remote", client_file=None, include_woo=False, started_by=None,
    )
    record = webui.RunRecord(run_id="rem1", command=[], mode="dry-run", target="remote")
    record.summary_path = "/srv/maintenance/logs/wp-update-summary-foo.json"
    record.mark_finished(0)
    webui._finalize_run_in_db(record)
    row = db_module.get_run(memory_db, webui_run_id="rem1")
    assert row["status"] == "success"
    assert row["ingest_status"] == "failed"


def test_start_run_raises_when_db_insert_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import db as db_module

    conn = db_module.open_db(":memory:")
    conn.close()  # any subsequent insert raises ProgrammingError → sqlite3.Error
    monkeypatch.setattr(webui, "DB_CONN", conn)

    with pytest.raises(RuntimeError, match="could not persist run"):
        webui.start_run(
            {"provider": "Cloudways", "target": "local"},
            webui.Settings(password="pw", secret="secret"),
        )
    # Live RUNS dict must NOT contain the run when persistence failed.
    with webui.RUNS_LOCK:
        assert not any(r.target == "local" and r.command for r in webui.RUNS.values()
                       if r.run_id not in {"live123", "rem1", "done1", "live1"})
