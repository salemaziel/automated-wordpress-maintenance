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
        ssh_config=Path("/dev/null"),
        ssh_key=None,
        connect_timeout=20,
        remote_timeout=600,
        http_timeout=20,
        max_consecutive_failures=3,
        stream=False,
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


def test_ssh_command_bypasses_system_config_by_default(tmp_path: Path) -> None:
    args = make_args(tmp_path)
    ssh_key = tmp_path / "id_rsa"
    ssh_key.write_text("dummy-key")
    updater = wp_update.WPUpdater(args)
    report = make_report(
        ssh_user="wpupdates-stage",
        ssh_key_path=str(ssh_key),
    )

    command, password = updater._ssh_cmd(report)

    assert password is None
    assert command[:3] == ["ssh", "-F", "/dev/null"]
    assert "-o" in command
    assert "BatchMode=yes" in command


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
# Tier-1 multi-candidate cascade (SSH_USER_CANDIDATES)
# ---------------------------------------------------------------------------

def _make_args_with_env(tmp_path: Path, env_body: str) -> argparse.Namespace:
    """Like make_args but lets the caller control the .env contents."""
    args = make_args(tmp_path)
    args.env_file.write_text(env_body)
    return args


def test_tier1_tries_all_candidates_until_one_succeeds(tmp_path: Path) -> None:
    args = _make_args_with_env(
        tmp_path,
        "SSH_USER=wpupdates\n"
        "SSH_USER_CANDIDATES=wpupdates-2,wpupdates-3\n"
        "SSH_KEY=\nAPP_PW=\n",
    )
    updater = wp_update.WPUpdater(args)
    r = _make_report_for_preflight(tmp_path)
    r.ssh_user = ""  # force the updater-level candidate list to drive ordering

    attempted: list[str] = []

    def fake_ssh(report: wp_update.SiteReport, script: str, timeout: object = None) -> str:
        attempted.append(report.ssh_user)
        if report.ssh_user in ("wpupdates", "wpupdates-2"):
            raise wp_update.SSHError("Permission denied (publickey)")
        return "ssh-ok"

    with patch.object(updater, "_ssh", side_effect=fake_ssh), \
         patch.object(updater, "_wp", return_value=""):
        updater._step_ssh_preflight(r)

    assert r.auth_method == "key"
    assert r.auth_user == "wpupdates-3"
    assert attempted == ["wpupdates", "wpupdates-2", "wpupdates-3"]


def test_tier1_falls_to_master_when_all_candidates_fail(tmp_path: Path) -> None:
    args = _make_args_with_env(
        tmp_path,
        "SSH_USER=wpupdates\n"
        "SSH_USER_CANDIDATES=wpupdates-2,wpupdates-3\n"
        "SSH_KEY=\nAPP_PW=\n",
    )
    updater = wp_update.WPUpdater(args)
    r = _make_report_for_preflight(tmp_path)
    r.ssh_user = ""

    def fake_ssh(report: wp_update.SiteReport, script: str, timeout: object = None) -> str:
        if report.auth_method == "key":
            raise wp_update.SSHError("Permission denied (publickey)")
        return "ssh-ok"

    with patch.object(updater, "_ssh", side_effect=fake_ssh), \
         patch.object(updater, "_wp", return_value=""):
        updater._step_ssh_preflight(r)

    assert r.auth_method == "master-key"
    assert r.auth_user == r.master_user


def test_tier1_reraises_on_nonpermission_error(tmp_path: Path) -> None:
    args = _make_args_with_env(
        tmp_path,
        "SSH_USER=wpupdates\n"
        "SSH_USER_CANDIDATES=wpupdates-2,wpupdates-3\n"
        "SSH_KEY=\nAPP_PW=\n",
    )
    updater = wp_update.WPUpdater(args)
    r = _make_report_for_preflight(tmp_path)
    r.ssh_user = ""

    attempted: list[str] = []

    def fake_ssh(report: wp_update.SiteReport, script: str, timeout: object = None) -> str:
        attempted.append(report.ssh_user)
        raise wp_update.SSHError("Connection timed out")

    with patch.object(updater, "_ssh", side_effect=fake_ssh), \
         patch.object(updater, "_wp", return_value=""), \
         pytest.raises(wp_update.SSHError, match="Connection timed out"):
        updater._step_ssh_preflight(r)

    # Only the first candidate should have been attempted; non-permission
    # failures short-circuit the cascade.
    assert attempted == ["wpupdates"]


def test_ssh_user_candidates_dedup_order(tmp_path: Path) -> None:
    args = _make_args_with_env(
        tmp_path,
        "SSH_USER=foo\n"
        "SSH_USER_CANDIDATES=foo, bar ,foo,baz\n"
        "SSH_KEY=\nAPP_PW=\n",
    )
    updater = wp_update.WPUpdater(args)
    assert updater._ssh_user_candidates == ["foo", "bar", "baz"]


def test_summary_includes_auth_user(tmp_path: Path) -> None:
    args = _make_args_with_env(
        tmp_path,
        "SSH_USER=wpupdates\n"
        "SSH_USER_CANDIDATES=wpupdates-stage\n"
        "SSH_KEY=\nAPP_PW=\n",
    )
    updater = wp_update.WPUpdater(args)
    r = _make_report_for_preflight(tmp_path)
    r.ssh_user = ""

    def fake_ssh(report: wp_update.SiteReport, script: str, timeout: object = None) -> str:
        if report.ssh_user == "wpupdates":
            raise wp_update.SSHError("Permission denied (publickey)")
        return "ssh-ok"

    with patch.object(updater, "_ssh", side_effect=fake_ssh), \
         patch.object(updater, "_wp", return_value=""):
        updater._step_ssh_preflight(r)

    serialised = r.to_dict()
    assert "auth_user" in serialised
    assert serialised["auth_user"] == "wpupdates-stage"


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
# Plugin-update flow — sequential updates with deactivate-on-fatal recovery
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


def test_run_plugin_update_structured_no_matching_entry(tmp_path: Path) -> None:
    """JSON parses cleanly but does not contain our slug — must be Error,
    never silent Up to date (regression guard)."""
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    # wp-cli returns an entry for a different plugin
    payload = '[{"name":"other-plugin","status":"Updated"}]'
    with patch.object(updater, "_wp", return_value=payload):
        result = updater._run_plugin_update_structured(r, "my-plugin")
    assert result["status"] == "Error"
    assert result.get("_no_entry") is True
    assert result["name"] == "my-plugin"


@pytest.mark.parametrize(
    "raw_status",
    ["Updated", "UPDATED", "updated", "Success", "success", "updated successfully",
     "Updated Successfully", "UPDATED SUCCESSFULLY"],
)
def test_run_plugin_update_structured_tolerant_status(tmp_path: Path, raw_status: str) -> None:
    """All these strings must classify as success after .strip().lower()."""
    assert raw_status.strip().lower() in wp_update._PLUGIN_STATUS_SUCCESS


@pytest.mark.parametrize(
    "raw_status",
    ["Up to date", "UP TO DATE", "up to date", "Already up to date",
     "already up to date"],
)
def test_run_plugin_update_structured_tolerant_uptodate(tmp_path: Path, raw_status: str) -> None:
    assert raw_status.strip().lower() in wp_update._PLUGIN_STATUS_UPTODATE


def test_step_update_plugins_success_continues(tmp_path: Path) -> None:
    """3 plugins, all update cleanly, verify passes after each."""
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.baseline = {
        "plugin_updates": [
            {"name": "plugin-a", "version": "1.0", "update_version": "2.0"},
            {"name": "plugin-b", "version": "1.0", "update_version": "2.0"},
            {"name": "plugin-c", "version": "1.0", "update_version": "2.0"},
        ]
    }

    def fake_structured(report: wp_update.SiteReport, slug: str) -> dict:
        return {"name": slug, "status": "Updated", "version": "2.0"}

    with (
        patch.object(updater, "_run_plugin_update_structured", side_effect=fake_structured),
        patch.object(updater, "_verify") as mock_verify,
        patch.object(updater, "_wp") as mock_wp,
    ):
        updater._step_update_plugins(r)

    # No deactivation should have run on the happy path
    mock_wp.assert_not_called()
    assert mock_verify.call_count == 3

    steps = {s.name: s for s in r.steps}
    assert steps["plugin-update:plugin-a"].status == "success"
    assert steps["plugin-update:plugin-b"].status == "success"
    assert steps["plugin-update:plugin-c"].status == "success"


def test_step_update_plugins_non_fatal_error_skips(tmp_path: Path) -> None:
    """plugin 2 returns Error (license failure) but verify passes → skipped,
    continues to plugin 3, no rollback."""
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.baseline = {
        "plugin_updates": [
            {"name": "plugin-a", "version": "1.0", "update_version": "2.0"},
            {"name": "plugin-b", "version": "1.0", "update_version": "2.0"},
            {"name": "plugin-c", "version": "1.0", "update_version": "2.0"},
        ]
    }

    def fake_structured(report: wp_update.SiteReport, slug: str) -> dict:
        if slug == "plugin-b":
            return {"name": slug, "status": "Error", "_exit_nonzero": True,
                    "_error": "license key invalid"}
        return {"name": slug, "status": "Updated", "version": "2.0"}

    with (
        patch.object(updater, "_run_plugin_update_structured", side_effect=fake_structured),
        patch.object(updater, "_verify"),  # always passes
        patch.object(updater, "_wp") as mock_wp,
    ):
        updater._step_update_plugins(r)  # must not raise

    mock_wp.assert_not_called()  # no deactivation needed

    steps = {s.name: s for s in r.steps}
    assert steps["plugin-update:plugin-a"].status == "success"
    assert steps["plugin-update:plugin-b"].status == "skipped"
    assert "non-fatal error" in steps["plugin-update:plugin-b"].detail
    assert steps["plugin-update:plugin-c"].status == "success"


def test_step_update_plugins_fatal_deactivation_recovers(tmp_path: Path) -> None:
    """plugin 2 update 'succeeds' but verify fails; wp plugin deactivate
    succeeds and subsequent verify passes → degraded, continues, no rollback."""
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.baseline = {
        "plugin_updates": [
            {"name": "plugin-a", "version": "1.0", "update_version": "2.0"},
            {"name": "plugin-b", "version": "1.0", "update_version": "2.0"},
            {"name": "plugin-c", "version": "1.0", "update_version": "2.0"},
        ]
    }

    verify_calls: list[str] = []
    # verify call sequence: a-post, b-post(FAIL), b-after-deactivate(OK), c-post
    verify_returns = iter([None, wp_update.HealthCheckError("500"), None, None])

    def fake_verify(report: wp_update.SiteReport) -> None:
        verify_calls.append("v")
        nxt = next(verify_returns)
        if isinstance(nxt, Exception):
            raise nxt

    wp_calls: list[str] = []

    def fake_wp(report: wp_update.SiteReport, cmd: str, timeout: int | None = None) -> str:
        wp_calls.append(cmd)
        return "Plugin deactivated."

    def fake_structured(report: wp_update.SiteReport, slug: str) -> dict:
        return {"name": slug, "status": "Updated", "version": "2.0"}

    with (
        patch.object(updater, "_run_plugin_update_structured", side_effect=fake_structured),
        patch.object(updater, "_verify", side_effect=fake_verify),
        patch.object(updater, "_wp", side_effect=fake_wp),
    ):
        updater._step_update_plugins(r)  # must not raise

    # Deactivate was invoked exactly once for plugin-b
    assert any("plugin deactivate" in c and "plugin-b" in c for c in wp_calls)
    # verify called 4 times (3 post-update + 1 post-deactivation)
    assert len(verify_calls) == 4

    steps = {s.name: s for s in r.steps}
    assert steps["plugin-update:plugin-a"].status == "success"
    assert steps["plugin-update:plugin-b"].status == "degraded"
    assert "deactivated" in steps["plugin-update:plugin-b"].detail
    assert steps["plugin-update:plugin-c"].status == "success"


def test_step_update_plugins_fatal_deactivation_fails_escalates(tmp_path: Path) -> None:
    """plugin 2 update succeeds but verify fails; deactivation runs but
    re-verify still fails → SSHError raised."""
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.baseline = {
        "plugin_updates": [
            {"name": "plugin-a", "version": "1.0", "update_version": "2.0"},
            {"name": "plugin-b", "version": "1.0", "update_version": "2.0"},
        ]
    }

    # verify: a-post(OK), b-post(FAIL), b-after-deactivate(FAIL)
    verify_returns = iter([None,
                           wp_update.HealthCheckError("500 after update"),
                           wp_update.HealthCheckError("still 500 after deactivate")])

    def fake_verify(report: wp_update.SiteReport) -> None:
        nxt = next(verify_returns)
        if isinstance(nxt, Exception):
            raise nxt

    def fake_structured(report: wp_update.SiteReport, slug: str) -> dict:
        return {"name": slug, "status": "Updated", "version": "2.0"}

    with (
        patch.object(updater, "_run_plugin_update_structured", side_effect=fake_structured),
        patch.object(updater, "_verify", side_effect=fake_verify),
        patch.object(updater, "_wp", return_value="Plugin deactivated."),
        pytest.raises(wp_update.SSHError, match="plugin plugin-b"),
    ):
        updater._step_update_plugins(r)

    steps = {s.name: s for s in r.steps}
    assert steps["plugin-update:plugin-a"].status == "success"
    assert steps["plugin-update:plugin-b"].status == "failed"


def test_step_update_plugins_verify_called_after_each(tmp_path: Path) -> None:
    """On the happy path, _verify is called exactly once per plugin."""
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    slugs = [f"plugin-{i}" for i in range(5)]
    r.baseline = {
        "plugin_updates": [
            {"name": s, "version": "1.0", "update_version": "2.0"} for s in slugs
        ]
    }

    with (
        patch.object(updater, "_run_plugin_update_structured",
                     side_effect=lambda rep, slug: {"name": slug, "status": "Updated"}),
        patch.object(updater, "_verify") as mock_verify,
    ):
        updater._step_update_plugins(r)

    assert mock_verify.call_count == len(slugs)


def test_extract_plugin_error_prefers_message(tmp_path: Path) -> None:
    assert wp_update._extract_plugin_error(
        {"name": "x", "status": "Error", "message": "license expired"}
    ) == "license expired"


def test_extract_plugin_error_truncates_long_raw() -> None:
    long = "x" * 500
    err = wp_update._extract_plugin_error({"name": "x", "status": "Error", "_raw": long})
    assert len(err) <= 200


def test_step_backup_still_creates_backup_dir_without_plugins_subdir(tmp_path: Path) -> None:
    """Regression: the plugins/ subdir mkdir has been removed; the main
    backup dir mkdir and all other backup steps remain."""
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.backup_dir = ""  # will be set by _step_backup

    captured: list[str] = []

    def fake_ssh(report: wp_update.SiteReport, script: str, **kw: object) -> str:
        captured.append(script)
        return "backup-ok"

    with patch.object(updater, "_ssh", side_effect=fake_ssh):
        updater._step_backup(r)

    assert captured
    script = captured[0]
    assert "mkdir -p" in script
    assert "preflight.sql" in script
    assert "public_html.tar.gz" in script
    # The per-plugin snapshot subdir is no longer created
    assert "/plugins'" not in script  # specifically the mkdir '…/plugins' line
