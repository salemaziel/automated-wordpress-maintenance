from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import wp_update


def make_args(
    tmp_path: Path,
    *,
    execute: bool = False,
    client_file: Path | None = None,
    clients_dir: Path | None = None,
) -> argparse.Namespace:
    env_file = tmp_path / ".env"
    env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=\n")
    return argparse.Namespace(
        execute=execute,
        env_file=env_file,
        clients_dir=clients_dir or (tmp_path / "clients"),
        client_file=client_file,
        log_dir=tmp_path / "logs",
        include_woocommerce=False,
        skip_staging=False,
        skip_ssl_verify=False,
        connect_timeout=20,
        remote_timeout=600,
        http_timeout=20,
        max_consecutive_failures=3,
        stream=False,
        prune_plugin_snapshots=False,
    )


def make_report(**overrides: object) -> wp_update.SiteReport:
    defaults = dict(
        client="Example Client",
        domain="example.com",
        server_ip="203.0.113.10",
        wp_path="/home/master/applications/abcd1234/public_html",
        is_staging=False,
        has_woocommerce=False,
    )
    defaults.update(overrides)
    return wp_update.SiteReport(**defaults)


class DummyResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode()

    def __enter__(self) -> DummyResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self, _size: int = -1) -> bytes:
        return self._body


def test_load_env_supports_export_quotes_and_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "export SSH_USER='wpupdates'\n"
        "SSH_KEY=~/keys/id_rsa\n"
        "APP_PW=$HOME/app-password\n"
        "BROKEN_LINE\n"
    )

    assert wp_update.load_env(env_file) == {
        "SSH_USER": "wpupdates",
        "SSH_KEY": str(home / "keys" / "id_rsa"),
        "APP_PW": str(home / "app-password"),
    }


def test_resolve_and_slugify_helpers() -> None:
    env = {"SSH_USER": "wpupdates"}

    assert wp_update.resolve("$SSH_USER", env) == "wpupdates"
    assert wp_update.resolve(" literal ", env) == "literal"
    assert wp_update.resolve(None, env) == ""

    assert wp_update.slugify("Acme Client / West") == "acme-client-west"
    assert wp_update.slugify("!!!") == "unknown"


def test_site_report_to_dict_omits_runtime_credentials() -> None:
    report = make_report(
        ssh_user="wpupdates",
        ssh_password="secret",
        ssh_key_path="/tmp/key",
        master_user="master_x",
        master_password="master-secret",
        steps=[
            wp_update.StepResult(
                name="baseline",
                status="success",
                started="2026-01-01T00:00:00+00:00",
                ended="2026-01-01T00:00:01+00:00",
                detail="ok",
            )
        ],
    )

    serialized = report.to_dict()

    assert serialized["client"] == "Example Client"
    assert serialized["steps"][0]["name"] == "baseline"
    assert "ssh_user" not in serialized
    assert "ssh_password" not in serialized
    assert "ssh_key_path" not in serialized
    assert "master_user" not in serialized
    assert "master_password" not in serialized


def test_gather_client_files_returns_sorted_cloudways_files(tmp_path: Path) -> None:
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    (clients_dir / "zeta_cloudways.json").write_text("{}")
    (clients_dir / "alpha_cloudways.json").write_text("{}")
    (clients_dir / "ignored.json").write_text("{}")

    updater = wp_update.WPUpdater(make_args(tmp_path, clients_dir=clients_dir))

    files = updater._gather_client_files()

    assert [path.name for path in files] == [
        "alpha_cloudways.json",
        "zeta_cloudways.json",
    ]


def test_validate_app_resolves_placeholders_from_env(tmp_path: Path) -> None:
    args = make_args(tmp_path)
    ssh_key = tmp_path / "id_rsa"
    ssh_key.write_text("dummy-key")
    args.env_file.write_text(
        "SSH_USER=wpupdates\n"
        f"SSH_KEY={ssh_key}\n"
        "APP_PW=app-password\n"
    )
    updater = wp_update.WPUpdater(args)

    doc = {
        "client_name": "Example Client",
        "server_ip_address": "203.0.113.10",
        "master_credentials": {"username": "master_x", "password": "master-secret"},
    }
    app = {
        "website_domain": "example.com",
        "path_to_public_html": "/home/master/applications/abcd1234/public_html",
        "sftp_credentials": {
            "username": "$SSH_USER",
            "password": "$APP_PW",
            "ssh_key": "$SSH_KEY",
        },
        "environment_flags": {"is_staging": True, "has_woocommerce": True},
    }

    report = updater._validate_app(doc, app, 1, "example-client_cloudways.json")

    assert report.ssh_user == "wpupdates"
    assert report.ssh_password == "app-password"
    assert report.ssh_key_path == str(ssh_key)
    assert report.master_user == "master_x"
    assert report.master_password == "master-secret"
    assert report.is_staging is True
    assert report.has_woocommerce is True


def test_compute_confidence_returns_full_score_when_nothing_needs_updates(tmp_path: Path) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))
    report = make_report(
        baseline={
            "plugin_updates": [],
            "theme_updates": [],
            "core_updates": [],
            "backup_plugins": [{"slug": "updraftplus"}],
            "disk": {"site_mb": 100, "available_mb": 10_000, "estimated_backup_mb": 60},
            "php_version": "8.2.12",
        }
    )

    confidence = updater._compute_confidence(report)

    assert confidence == {
        "score": 100,
        "grade": "HIGH",
        "factors": ["     No updates pending — nothing to change"],
    }


def test_compute_confidence_accumulates_risk_factors(tmp_path: Path) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))
    report = make_report(
        has_woocommerce=True,
        baseline={
            "plugin_updates": [{} for _ in range(6)],
            "theme_updates": [{}],
            "core_updates": [{"version": "6.8"}],
            "backup_plugins": [],
            "disk": {"site_mb": 2500, "available_mb": 500, "estimated_backup_mb": 200},
            "php_version": "7.4.33",
        },
    )

    confidence = updater._compute_confidence(report)

    assert confidence["score"] == 35
    assert confidence["grade"] == "RISKY"
    assert "-15  WooCommerce site (payment/order risk)" in confidence["factors"]
    assert "-10  6 plugin updates (>5)" in confidence["factors"]
    assert "-10  Outdated PHP 7.4.33 (<8.0)" in confidence["factors"]


def test_http_check_accepts_4xx_if_another_endpoint_is_healthy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        if req.full_url == "https://example.com":
            raise wp_update.urlerror.HTTPError(req.full_url, 404, "Not Found", None, None)
        return DummyResponse(200, "login ok")

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)

    assert updater._http_check("example.com") == "ok"


def test_http_check_flags_fatal_error_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        return DummyResponse(200, "There has been a critical error on this website")

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)

    result = updater._http_check("https://example.com")

    assert "fatal marker" in result
    assert "critical error" in result


# ---------------------------------------------------------------------------
# VALID_PATH regex edge cases
# ---------------------------------------------------------------------------

def test_valid_path_accepts_hash_only() -> None:
    assert wp_update.VALID_PATH.match(
        "/home/master/applications/abcd1234/public_html"
    )


def test_valid_path_accepts_staging_suffix() -> None:
    assert wp_update.VALID_PATH.match(
        "/home/master/applications/abcd1234-staging/public_html"
    )


def test_valid_path_accepts_underscores() -> None:
    assert wp_update.VALID_PATH.match(
        "/home/master/applications/abc_def_123/public_html"
    )


def test_valid_path_rejects_empty_string() -> None:
    assert not wp_update.VALID_PATH.match("")


def test_valid_path_rejects_root() -> None:
    assert not wp_update.VALID_PATH.match("/")


def test_valid_path_rejects_dotdot_traversal() -> None:
    assert not wp_update.VALID_PATH.match(
        "/home/master/applications/../etc/public_html"
    )


def test_valid_path_rejects_semicolon_in_hash() -> None:
    assert not wp_update.VALID_PATH.match(
        "/home/master/applications/abc;rm -rf/public_html"
    )


def test_valid_path_rejects_dollar_sign_in_hash() -> None:
    assert not wp_update.VALID_PATH.match(
        "/home/master/applications/abc$USER/public_html"
    )


def test_valid_path_rejects_space_in_hash() -> None:
    assert not wp_update.VALID_PATH.match(
        "/home/master/applications/abc def/public_html"
    )


def test_valid_path_rejects_dot_in_hash() -> None:
    # Cloudways app dirs are hashes, not domain names; dots are not permitted
    assert not wp_update.VALID_PATH.match(
        "/home/master/applications/example.com/public_html"
    )


def test_valid_path_rejects_trailing_slash() -> None:
    assert not wp_update.VALID_PATH.match(
        "/home/master/applications/abcd1234/public_html/"
    )


# ---------------------------------------------------------------------------
# _http_check — connection error and timeout return error strings
# ---------------------------------------------------------------------------

def test_http_check_connection_error_returns_error_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        raise OSError("Connection refused")

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)

    result = updater._http_check("example.com")

    assert result != "ok"
    assert "Connection refused" in result


def test_http_check_timeout_returns_error_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        raise TimeoutError("timed out")

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)

    result = updater._http_check("example.com")

    assert result != "ok"
    assert "timed out" in result.lower()


def test_http_check_retries_on_transient_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))
    call_count = 0

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("transient network hiccup")
        return DummyResponse(200, "all good")

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)

    result = updater._http_check("example.com")

    assert result == "ok"
    assert call_count >= 2


# ---------------------------------------------------------------------------
# _step_ssh_preflight — three-tier auth cascade
# ---------------------------------------------------------------------------

def _make_report_for_preflight(tmp_path: Path) -> wp_update.SiteReport:
    ssh_key = tmp_path / "id_rsa"
    ssh_key.write_text("dummy-key")
    return make_report(
        ssh_user="wpupdates",
        ssh_password="",
        ssh_key_path=str(ssh_key),
        master_user="master_abc",
        master_password="master-secret",
    )


def test_ssh_preflight_tier1_success(tmp_path: Path) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))
    r = _make_report_for_preflight(tmp_path)

    with patch.object(updater, "_ssh", return_value="ssh-ok"), \
         patch.object(updater, "_wp", return_value=""):
        updater._step_ssh_preflight(r)

    assert r.auth_method == "key"
    assert any(s.name == "ssh-preflight" and s.status == "success" for s in r.steps)


def test_ssh_preflight_tier2_used_when_tier1_permission_denied(tmp_path: Path) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))
    r = _make_report_for_preflight(tmp_path)

    ssh_calls = []

    def fake_ssh(report, script, timeout=None):  # noqa: ANN001
        ssh_calls.append(report.auth_method)
        if report.auth_method == "key":
            raise wp_update.SSHError("Permission denied (publickey)")
        return "ssh-ok"

    with patch.object(updater, "_ssh", side_effect=fake_ssh), \
         patch.object(updater, "_wp", return_value=""):
        updater._step_ssh_preflight(r)

    assert r.auth_method == "master-key"
    assert any(s.name == "ssh-preflight" and s.status == "success" for s in r.steps)


def test_ssh_preflight_tier3_used_when_tier2_fails(tmp_path: Path) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))
    r = _make_report_for_preflight(tmp_path)

    def fake_ssh(report, script, timeout=None):  # noqa: ANN001
        if report.auth_method == "key":
            raise wp_update.SSHError("Permission denied (publickey)")
        if report.auth_method == "master-key":
            raise wp_update.SSHError("Permission denied (publickey)")
        return "ssh-ok"

    with patch.object(updater, "_ssh", side_effect=fake_ssh), \
         patch.object(updater, "_wp", return_value=""), \
         patch("wp_update.shutil.which", return_value="/usr/bin/sshpass"):
        updater._step_ssh_preflight(r)

    assert r.auth_method == "master"
    assert any(s.name == "ssh-preflight" and s.status == "success" for s in r.steps)


def test_ssh_preflight_non_permission_error_reraises(tmp_path: Path) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))
    r = _make_report_for_preflight(tmp_path)

    with (
        patch.object(updater, "_ssh", side_effect=wp_update.SSHError("Connection timed out")),
        pytest.raises(wp_update.SSHError, match="Connection timed out"),
    ):
        updater._step_ssh_preflight(r)


def test_ssh_preflight_raises_when_no_master_credentials(tmp_path: Path) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))
    r = _make_report_for_preflight(tmp_path)
    r.master_user = ""
    r.master_password = ""

    with (
        patch.object(updater, "_ssh", side_effect=wp_update.SSHError("Permission denied")),
        pytest.raises(wp_update.SSHError, match="no master"),
    ):
        updater._step_ssh_preflight(r)


# ---------------------------------------------------------------------------
# _step_rollback — success and failure paths
# ---------------------------------------------------------------------------

def test_step_rollback_success_constructs_correct_script(tmp_path: Path) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))
    r = make_report(
        backup_dir="/home/master/wp-maintenance-backups/example-client/example.com/run01",
        auth_method="key",
    )

    captured_scripts = []

    def fake_ssh(report, script, timeout=None):  # noqa: ANN001
        captured_scripts.append(script)
        return "rollback-ok"

    with patch.object(updater, "_ssh", side_effect=fake_ssh), \
         patch.object(updater, "_verify", return_value=None):
        updater._step_rollback(r)

    assert r.overall == "rolled-back"
    assert r.rollback_result == "success"
    assert any(s.name == "rollback" and s.status == "success" for s in r.steps)

    script_text = captured_scripts[0]
    assert "failed-state.tar.gz" in script_text
    assert "find" in script_text and "-mindepth 1" in script_text
    assert "db import" in script_text
    assert "tar -xzf" in script_text


def test_step_rollback_failure_raises_and_sets_failed_state(tmp_path: Path) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))
    r = make_report(
        backup_dir="/home/master/wp-maintenance-backups/example-client/example.com/run01",
        auth_method="key",
    )

    with (
        patch.object(updater, "_ssh", side_effect=wp_update.SSHError("connection lost")),
        pytest.raises(
            wp_update.RollbackFailed,
            match="rollback failed for example.com: connection lost",
        ),
    ):
        updater._step_rollback(r)

    assert r.overall == "failed"
    assert r.rollback_result.startswith("FAILED:")
    assert "connection lost" in r.rollback_result
    assert any(s.name == "rollback" and s.status == "failed" for s in r.steps)


# ---------------------------------------------------------------------------
# _process_client_file — staging gate skips production sites on staging failure
# ---------------------------------------------------------------------------

def _make_client_json(tmp_path: Path) -> Path:
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir(exist_ok=True)
    doc = {
        "client_name": "Test Client",
        "server_ip_address": "203.0.113.50",
        "master_credentials": {"username": "master_xyz", "password": "pw"},
        "applications": [
            {
                "website_domain": "staging.example.com",
                "path_to_public_html": "/home/master/applications/stag1234/public_html",
                "sftp_credentials": {"username": "$SSH_USER", "password": "$APP_PW", "ssh_key": "$SSH_KEY"},
                "environment_flags": {"is_staging": True, "has_woocommerce": False},
            },
            {
                "website_domain": "example.com",
                "path_to_public_html": "/home/master/applications/prod1234/public_html",
                "sftp_credentials": {"username": "$SSH_USER", "password": "$APP_PW", "ssh_key": "$SSH_KEY"},
                "environment_flags": {"is_staging": False, "has_woocommerce": False},
            },
            {
                "website_domain": "example2.com",
                "path_to_public_html": "/home/master/applications/prod5678/public_html",
                "sftp_credentials": {"username": "$SSH_USER", "password": "$APP_PW", "ssh_key": "$SSH_KEY"},
                "environment_flags": {"is_staging": False, "has_woocommerce": False},
            },
        ],
    }
    path = clients_dir / "test-client_cloudways.json"
    path.write_text(json.dumps(doc))
    return path


def _write_single_app_client_json(
    clients_dir: Path, filename: str, domain: str, app_hash: str
) -> Path:
    doc = {
        "client_name": filename.removesuffix("_cloudways.json").replace("-", " ").title(),
        "server_ip_address": "203.0.113.50",
        "master_credentials": {"username": "master_xyz", "password": "pw"},
        "applications": [
            {
                "website_domain": domain,
                "path_to_public_html": f"/home/master/applications/{app_hash}/public_html",
                "sftp_credentials": {
                    "username": "$SSH_USER",
                    "password": "$APP_PW",
                    "ssh_key": "$SSH_KEY",
                },
                "environment_flags": {"is_staging": False, "has_woocommerce": False},
            }
        ],
    }
    path = clients_dir / filename
    path.write_text(json.dumps(doc))
    return path


def test_process_client_file_skips_prod_sites_when_staging_fails(tmp_path: Path) -> None:
    args = make_args(tmp_path, execute=True)
    # WPUpdater __init__ requires either SSH_KEY or APP_PW in execute mode.
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=fake-password-for-test\n")
    updater = wp_update.WPUpdater(args)

    client_file = _make_client_json(tmp_path)

    def fake_process_site(report):  # noqa: ANN001
        if report.is_staging:
            report.overall = "failed"
            report.failure_detail = "simulated staging failure"

    mock_process_site = MagicMock(side_effect=fake_process_site)
    with patch.object(updater, "_process_site", mock_process_site):
        updater._process_client_file(client_file)

    assert mock_process_site.call_count == 1, (
        "_process_site should only be called for the staging site"
    )

    prod_reports = [r for r in updater.reports if not r.is_staging]
    assert len(prod_reports) == 2

    for prod_report in prod_reports:
        assert prod_report.overall == "skipped", (
            f"Expected prod site {prod_report.domain} to be skipped but got {prod_report.overall!r}"
        )
        assert any(
            s.name == "staging-gate" and s.status == "skipped"
            for s in prod_report.steps
        ), f"Expected staging-gate step on {prod_report.domain}"


def test_run_returns_failure_when_rollback_failure_aborts_batch(tmp_path: Path) -> None:
    args = make_args(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=fake-password-for-test\n")
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    args.clients_dir = clients_dir

    _write_single_app_client_json(
        clients_dir, "client-a_cloudways.json", "a.example.com", "apphasha"
    )
    _write_single_app_client_json(
        clients_dir, "client-b_cloudways.json", "b.example.com", "apphashb"
    )

    updater = wp_update.WPUpdater(args)

    def fake_process_site(report):  # noqa: ANN001
        report.overall = "failed"
        report.rollback_result = "FAILED: connection lost"
        raise wp_update.RollbackFailed(
            f"rollback failed for {report.domain}: connection lost"
        )

    with patch.object(updater, "_process_site", side_effect=fake_process_site):
        rc = updater.run()

    assert rc == 1
    assert updater._run_abort_reason == "rollback failed for a.example.com: connection lost"
    assert [r.domain for r in updater.reports] == ["a.example.com"]


def test_run_aborts_after_max_consecutive_failures(tmp_path: Path) -> None:
    args = make_args(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=fake-password-for-test\n")
    args.max_consecutive_failures = 2
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    args.clients_dir = clients_dir

    _write_single_app_client_json(
        clients_dir, "client-a_cloudways.json", "a.example.com", "apphasha"
    )
    _write_single_app_client_json(
        clients_dir, "client-b_cloudways.json", "b.example.com", "apphashb"
    )
    _write_single_app_client_json(
        clients_dir, "client-c_cloudways.json", "c.example.com", "apphashc"
    )

    updater = wp_update.WPUpdater(args)
    outcomes = iter(("failed", "rolled-back", "success"))

    def fake_process_site(report):  # noqa: ANN001
        report.overall = next(outcomes)
        report.failure_step = "final-verification"
        if report.overall != "success":
            report.failure_detail = "simulated outage"

    with patch.object(updater, "_process_site", side_effect=fake_process_site):
        rc = updater.run()

    assert rc == 1
    assert updater._run_abort_reason == (
        "circuit breaker opened after 2 consecutive failed/rolled-back site(s)"
    )
    assert [r.domain for r in updater.reports] == ["a.example.com", "b.example.com"]


# ---------------------------------------------------------------------------
# Part 1 — Structured plugin-update failure detection
# ---------------------------------------------------------------------------

def _make_updater(tmp_path: Path, *, execute: bool = True) -> wp_update.WPUpdater:
    args = make_args(tmp_path, execute=execute)
    # Write a non-empty APP_PW so execute-mode validation passes
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=fake-pw\n")
    return wp_update.WPUpdater(args)


def _make_exec_report(**overrides: object) -> wp_update.SiteReport:
    defaults = dict(
        client="Test Client",
        domain="example.com",
        server_ip="203.0.113.1",
        wp_path="/home/master/applications/abc123/public_html",
        is_staging=False,
        has_woocommerce=False,
        backup_dir="/home/master/applications/abc123/private_html/wp-maintenance-backups/run1",
    )
    defaults.update(overrides)
    return wp_update.SiteReport(**defaults)


def test_plugin_dir_fingerprint_returns_consistent_string(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    expected = "deadbeef" * 8  # 64-char hex string
    with patch.object(updater, "_ssh", return_value=expected) as mock_ssh:
        result = updater._plugin_dir_fingerprint(r, "my-plugin")
    assert result == expected
    assert mock_ssh.call_count == 1
    script = mock_ssh.call_args[0][1]
    assert "my-plugin" in script
    assert "sha256sum" in script
    assert "LC_ALL=C" in script


def test_plugin_dir_fingerprint_empty_when_dir_missing(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    with patch.object(updater, "_ssh", return_value=""):
        result = updater._plugin_dir_fingerprint(r, "missing-plugin")
    assert result == ""


def test_run_plugin_update_structured_parses_clean_json(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    payload = '[{"name":"my-plugin","status":"Updated","version":"1.0","update_version":"1.1"}]'
    with patch.object(updater, "_wp", return_value=payload):
        result = updater._run_plugin_update_structured(r, "my-plugin")
    assert result["status"] == "Updated"
    assert result["name"] == "my-plugin"


def test_run_plugin_update_structured_strips_php_warnings(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    raw = (
        "PHP Warning: some-warning in /path/to/file.php on line 42\n"
        '[{"name":"my-plugin","status":"Updated","version":"1.0","update_version":"2.0"}]'
    )
    with patch.object(updater, "_wp", return_value=raw):
        result = updater._run_plugin_update_structured(r, "my-plugin")
    assert result["status"] == "Updated"


def test_run_plugin_update_structured_returns_error_on_malformed_json(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    with patch.object(updater, "_wp", return_value="garbage output no json"):
        result = updater._run_plugin_update_structured(r, "my-plugin")
    assert result["status"] == "Error"
    assert result.get("_parse_error")


def test_run_plugin_update_structured_returns_error_on_ssh_failure(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    with patch.object(updater, "_wp", side_effect=wp_update.SSHError("connection refused")):
        result = updater._run_plugin_update_structured(r, "my-plugin")
    assert result["status"] == "Error"
    assert result.get("_exit_nonzero")


def test_step_update_plugins_records_success_from_json_status_updated(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.baseline = {
        "plugin_updates": [{"name": "good-plugin", "version": "1.0", "update_version": "2.0"}]
    }
    fp = "aabbcc" * 10

    with (
        patch.object(updater, "_snapshot_plugin"),
        patch.object(updater, "_plugin_dir_fingerprint", return_value=fp),
        patch.object(updater, "_run_plugin_update_structured",
                     return_value={"name": "good-plugin", "status": "Updated"}),
        patch.object(updater, "_verify"),
    ):
        updater._step_update_plugins(r)

    steps = {s.name: s for s in r.steps}
    assert "plugin-update:good-plugin" in steps
    assert steps["plugin-update:good-plugin"].status == "success"


def test_step_update_plugins_records_skipped_on_error_with_unchanged_fingerprint(
    tmp_path: Path,
) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.baseline = {
        "plugin_updates": [{"name": "perfmatters", "version": "2.0", "update_version": "2.1"}]
    }
    fp = "aabbcc" * 10  # same before and after

    with (
        patch.object(updater, "_snapshot_plugin"),
        patch.object(updater, "_plugin_dir_fingerprint", return_value=fp),
        patch.object(updater, "_run_plugin_update_structured",
                     return_value={"name": "perfmatters", "status": "Error",
                                   "_exit_nonzero": True}),
        patch.object(updater, "_verify"),
    ):
        updater._step_update_plugins(r)  # must not raise

    steps = {s.name: s for s in r.steps}
    assert steps["plugin-update:perfmatters"].status == "skipped"
    assert "fingerprint unchanged" in steps["plugin-update:perfmatters"].detail


def test_step_update_plugins_delegates_to_per_plugin_restore_on_changed_fingerprint(
    tmp_path: Path,
) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.baseline = {
        "plugin_updates": [
            {"name": "plugin-a", "version": "1.0", "update_version": "2.0"},
            {"name": "plugin-b", "version": "1.0", "update_version": "2.0"},
            {"name": "plugin-c", "version": "1.0", "update_version": "2.0"},
        ]
    }
    fps: dict[str, list[str]] = {
        "plugin-a": ["fp-a", "fp-a"],
        "plugin-b": ["fp-b-before", "fp-b-after"],  # changed
        "plugin-c": ["fp-c", "fp-c"],
    }
    call_counts: dict[str, int] = {"plugin-a": 0, "plugin-b": 0, "plugin-c": 0}

    def fake_fingerprint(report: wp_update.SiteReport, slug: str) -> str:
        call_counts[slug] += 1
        return fps[slug][(call_counts[slug] - 1) % 2]

    def fake_structured(report: wp_update.SiteReport, slug: str) -> dict:
        if slug == "plugin-b":
            return {"name": slug, "status": "Error", "_exit_nonzero": True}
        return {"name": slug, "status": "Updated"}

    with (
        patch.object(updater, "_snapshot_plugin"),
        patch.object(updater, "_plugin_dir_fingerprint", side_effect=fake_fingerprint),
        patch.object(updater, "_run_plugin_update_structured", side_effect=fake_structured),
        patch.object(updater, "_verify"),
        patch.object(updater, "_try_restore_plugin", return_value=True) as mock_restore,
    ):
        updater._step_update_plugins(r)

    mock_restore.assert_called_once()
    assert mock_restore.call_args[0][1] == "plugin-b"

    steps = {s.name: s for s in r.steps}
    assert steps["plugin-update:plugin-a"].status == "success"
    assert steps["plugin-update:plugin-b"].status == "rolled-back-local"
    assert steps["plugin-update:plugin-c"].status == "success"


def test_step_update_plugins_propagates_when_per_plugin_restore_fails(
    tmp_path: Path,
) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.baseline = {
        "plugin_updates": [{"name": "bad-plugin", "version": "1.0", "update_version": "2.0"}]
    }

    with (
        patch.object(updater, "_snapshot_plugin"),
        patch.object(updater, "_plugin_dir_fingerprint",
                     side_effect=["fp-before", "fp-after"]),
        patch.object(updater, "_run_plugin_update_structured",
                     return_value={"name": "bad-plugin", "status": "Error",
                                   "_exit_nonzero": True}),
        patch.object(updater, "_try_restore_plugin", return_value=False),
        pytest.raises(wp_update.SSHError, match="per-plugin restore failed"),
    ):
        updater._step_update_plugins(r)

    steps = {s.name: s for s in r.steps}
    assert steps["plugin-update:bad-plugin"].status == "failed"


# ---------------------------------------------------------------------------
# Part 2 — Per-plugin incremental snapshots
# ---------------------------------------------------------------------------

def test_step_backup_creates_plugins_subdir_in_script(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.backup_dir = None  # will be set by _step_backup

    captured: list[str] = []

    def fake_ssh(report: wp_update.SiteReport, script: str, **kw: object) -> str:
        captured.append(script)
        return "backup-ok"

    with patch.object(updater, "_ssh", side_effect=fake_ssh):
        updater._step_backup(r)

    assert captured, "expected _ssh to be called"
    script = captured[0]
    assert "/plugins" in script
    assert "mkdir -p" in script


def test_snapshot_plugin_tars_slug_from_plugins_root(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    captured: list[str] = []

    def fake_ssh(report: wp_update.SiteReport, script: str, **kw: object) -> str:
        captured.append(script)
        return ""

    with patch.object(updater, "_ssh", side_effect=fake_ssh):
        updater._snapshot_plugin(r, "my-plugin")

    assert captured
    script = captured[0]
    assert "wp-content/plugins" in script
    assert "my-plugin.tar.gz" in script
    assert "tar -czf" in script


def test_snapshot_plugin_records_skipped_when_dir_missing(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()

    with patch.object(updater, "_ssh", return_value="no-plugin-dir"):
        updater._snapshot_plugin(r, "gone-plugin")

    steps = {s.name: s for s in r.steps}
    assert steps["plugin-snapshot:gone-plugin"].status == "skipped"


def test_try_restore_plugin_success_returns_true(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()

    with (
        patch.object(updater, "_ssh", return_value=""),
        patch.object(updater, "_verify"),
    ):
        result = updater._try_restore_plugin(r, "my-plugin")

    assert result is True
    steps = {s.name: s for s in r.steps}
    assert steps["plugin-restore:my-plugin"].status == "success"


def test_try_restore_plugin_returns_false_when_tarball_missing(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()

    with patch.object(updater, "_ssh",
                      side_effect=wp_update.SSHError("exit=1: no-tarball")):
        result = updater._try_restore_plugin(r, "missing-plugin")

    assert result is False
    steps = {s.name: s for s in r.steps}
    assert steps["plugin-restore:missing-plugin"].status == "failed"


def test_try_restore_plugin_returns_false_when_verify_fails(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()

    with (
        patch.object(updater, "_ssh", return_value=""),
        patch.object(updater, "_verify",
                     side_effect=wp_update.HealthCheckError("site down after restore")),
    ):
        result = updater._try_restore_plugin(r, "broken-plugin")

    assert result is False
    steps = {s.name: s for s in r.steps}
    assert steps["plugin-restore:broken-plugin"].status == "failed"
    assert "verify" in steps["plugin-restore:broken-plugin"].detail


def test_try_restore_plugin_returns_false_when_no_backup_dir(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = make_report()  # no backup_dir set

    with patch.object(updater, "_ssh") as mock_ssh:
        result = updater._try_restore_plugin(r, "any-plugin")

    assert result is False
    mock_ssh.assert_not_called()
