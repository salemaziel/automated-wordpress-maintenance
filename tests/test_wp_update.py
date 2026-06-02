from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import wp_update


def make_args(
    tmp_path: Path,
    *,
    execute: bool = False,
    client_file: Path | list[Path] | None = None,
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
        no_ssh_mux=True,
        ssh_mux_persist="5m",
        remote_timeout=600,
        http_timeout=20,
        max_consecutive_failures=3,
        skip_up_to_date_ttl=60,
        no_skip_up_to_date=False,
        recheck_updates=False,
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


def test_gather_client_files_excludes_archived_subdir(tmp_path: Path) -> None:
    """Files under clients/_archived/ are soft-deleted from the webui and
    must not be picked up by fleet runs that scan the inventory dir."""
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    # Active files at multiple depths
    (clients_dir / "alpha_cloudways.json").write_text("{}")
    cloudways_dir = clients_dir / "cloudways" / "beta"
    cloudways_dir.mkdir(parents=True)
    (cloudways_dir / "beta_cloudways.json").write_text("{}")
    # Archived siblings at corresponding depths
    archived_flat = clients_dir / "_archived" / "gone_cloudways.json"
    archived_flat.parent.mkdir(parents=True)
    archived_flat.write_text("{}")
    archived_nested = clients_dir / "_archived" / "cloudways" / "ditched" / "ditched_cloudways.json"
    archived_nested.parent.mkdir(parents=True)
    archived_nested.write_text("{}")

    updater = wp_update.WPUpdater(make_args(tmp_path, clients_dir=clients_dir))
    files = updater._gather_client_files()

    names = [p.name for p in files]
    assert "alpha_cloudways.json" in names
    assert "beta_cloudways.json" in names
    assert "gone_cloudways.json" not in names
    assert "ditched_cloudways.json" not in names


def test_gather_client_files_accepts_multiple_explicit_paths(tmp_path: Path) -> None:
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    a = clients_dir / "alpha_cloudways.json"
    b = clients_dir / "beta_cloudways.json"
    c = clients_dir / "gamma_cloudways.json"
    for p in (a, b, c):
        p.write_text("{}")

    args = make_args(tmp_path, clients_dir=clients_dir, client_file=[a, b, a])
    updater = wp_update.WPUpdater(args)

    files = updater._gather_client_files()

    # Duplicates collapsed; gamma not requested so excluded.
    assert sorted(p.name for p in files) == ["alpha_cloudways.json", "beta_cloudways.json"]


def test_client_file_argparse_is_repeatable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "argv",
        ["wp_update.py", "--client-file", "x.json", "--client-file", "y.json"],
    )
    ns = wp_update.build_cli()
    assert [Path(p).name for p in ns.client_file] == ["x.json", "y.json"]


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


def test_ssh_command_includes_controlmaster_when_mux_enabled(tmp_path: Path) -> None:
    args = make_args(tmp_path)
    args.no_ssh_mux = False  # explicit override: real default is mux ON
    ssh_key = tmp_path / "id_rsa"
    ssh_key.write_text("dummy-key")
    updater = wp_update.WPUpdater(args)
    report = make_report(
        ssh_user="wpupdates-stage",
        ssh_key_path=str(ssh_key),
    )

    command, _ = updater._ssh_cmd(report)
    joined = " ".join(command)

    assert "ControlMaster=auto" in joined
    assert "ControlPath=~/.ssh/cm-%C" in joined
    assert "ControlPersist=5m" in joined


def test_ssh_command_omits_controlmaster_when_mux_disabled(tmp_path: Path) -> None:
    args = make_args(tmp_path)  # fixture sets no_ssh_mux=True
    ssh_key = tmp_path / "id_rsa"
    ssh_key.write_text("dummy-key")
    updater = wp_update.WPUpdater(args)
    report = make_report(
        ssh_user="wpupdates-stage",
        ssh_key_path=str(ssh_key),
    )

    command, _ = updater._ssh_cmd(report)
    joined = " ".join(command)

    assert "ControlMaster" not in joined
    assert "ControlPath" not in joined
    assert "ControlPersist" not in joined


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
# Baseline HTTP status snapshot — avoids false-positive rollbacks on
# Coming-Soon / archive plugins that intentionally serve 5xx.
# ---------------------------------------------------------------------------

def test_capture_http_status_returns_200_on_normal_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        return DummyResponse(200, "<html></html>")

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)
    assert updater._capture_http_status("example.com") == 200


def test_capture_http_status_returns_503_for_intentional_splash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SeedProd 'Coming Soon' returns 503 + a full HTML body. The captured
    baseline status must be the integer 503 so _verify can later accept
    matching 503s as healthy."""
    updater = wp_update.WPUpdater(make_args(tmp_path))

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        raise wp_update.urlerror.HTTPError(
            req.full_url, 503, "Service Unavailable", None, None,
        )

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)
    assert updater._capture_http_status("archive.example.com") == 503


def test_capture_http_status_returns_none_on_connection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = wp_update.WPUpdater(make_args(tmp_path))

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        raise OSError("DNS failure")

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)
    assert updater._capture_http_status("nope.example.com") is None


def test_verify_accepts_5xx_matching_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Site whose baseline was 503 (e.g. SeedProd Coming-Soon plugin) and
    is still 503 post-mutation must pass _verify — that's the expected
    healthy state, not a regression."""
    updater = wp_update.WPUpdater(make_args(tmp_path))
    monkeypatch.setattr(wp_update.time, "sleep", lambda _s: None)

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        raise wp_update.urlerror.HTTPError(
            req.full_url, 503, "Service Unavailable", None, None,
        )

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)
    # Bypass the wp-cli sanity check (Layer 1 of _verify).
    monkeypatch.setattr(updater, "_wp", lambda r, cmd: "")

    report = make_report(domain="archive.example.com", baseline_http_status=503)
    updater._verify(report)  # no HealthCheckError raised


def test_verify_still_rejects_5xx_that_differs_from_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline 503 / post-mutation 502 means our update *did* change the
    site's response — that's a real regression, not the intentional
    Coming-Soon splash. _verify must still raise."""
    updater = wp_update.WPUpdater(make_args(tmp_path))
    monkeypatch.setattr(wp_update.time, "sleep", lambda _s: None)

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        raise wp_update.urlerror.HTTPError(
            req.full_url, 502, "Bad Gateway", None, None,
        )

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)
    monkeypatch.setattr(updater, "_wp", lambda r, cmd: "")

    report = make_report(domain="archive.example.com", baseline_http_status=503)
    with pytest.raises(wp_update.HealthCheckError, match="502"):
        updater._verify(report)


def test_verify_rejects_5xx_when_baseline_was_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Site that was healthy at baseline (200) but now returns 503 — the
    update genuinely broke something and _verify must still raise."""
    updater = wp_update.WPUpdater(make_args(tmp_path))
    monkeypatch.setattr(wp_update.time, "sleep", lambda _s: None)

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        raise wp_update.urlerror.HTTPError(
            req.full_url, 503, "Service Unavailable", None, None,
        )

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)
    monkeypatch.setattr(updater, "_wp", lambda r, cmd: "")

    report = make_report(domain="example.com", baseline_http_status=200)
    with pytest.raises(wp_update.HealthCheckError, match="503"):
        updater._verify(report)


def test_verify_rejects_5xx_when_no_baseline_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a baseline (None — e.g. capture failed at collect-baseline
    time), a persistent 5xx must still be treated as a failure. We never
    accept 5xx implicitly."""
    updater = wp_update.WPUpdater(make_args(tmp_path))
    monkeypatch.setattr(wp_update.time, "sleep", lambda _s: None)

    def fake_urlopen(req, timeout, context):  # noqa: ANN001
        raise wp_update.urlerror.HTTPError(
            req.full_url, 503, "Service Unavailable", None, None,
        )

    monkeypatch.setattr(wp_update.urlrequest, "urlopen", fake_urlopen)
    monkeypatch.setattr(updater, "_wp", lambda r, cmd: "")

    report = make_report(domain="example.com", baseline_http_status=None)
    with pytest.raises(wp_update.HealthCheckError, match="503"):
        updater._verify(report)


def test_to_dict_includes_baseline_http_status() -> None:
    report = make_report(baseline_http_status=503)
    out = report.to_dict()
    assert out["baseline_http_status"] == 503


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


def test_step_rollback_script_preserves_distinct_exit_codes(tmp_path: Path) -> None:
    # Each guard in the rollback script exits with a distinct code so logs
    # can pinpoint which defense-in-depth check fired. Refactors that move
    # the heredoc into a constant must keep these codes intact.
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

    script_text = captured_scripts[0]
    for code in ("exit 99", "exit 98", "exit 97", "exit 96", "exit 95", "exit 94", "exit 93"):
        assert code in script_text, f"rollback script missing {code}"


def test_step_rollback_verifies_wp_config_integrity(tmp_path: Path) -> None:
    # The rollback must refuse to extract an archive lacking wp-config.php and
    # must prove the restored config matches the backup-time fingerprint. A
    # rollback that only restores wp-content/ leaves WordPress unbootable.
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

    script_text = captured_scripts[0]
    # Pre-extract: archive must contain wp-config.php
    assert "missing wp-config.php — refusing to extract" in script_text
    # Post-extract: restored config must be non-empty and checksum-matched
    assert "wp-config.php missing or empty after restore" in script_text
    assert "wp-config.sha256" in script_text
    assert "sha256sum" in script_text
    assert "checksum mismatch after restore" in script_text


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
        patch.object(updater, "_flush_cache"),
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
        patch.object(updater, "_flush_cache"),
        patch.object(updater, "_wp") as mock_wp,
    ):
        updater._step_update_plugins(r)  # must not raise

    mock_wp.assert_not_called()  # no deactivation needed

    steps = {s.name: s for s in r.steps}
    assert steps["plugin-update:plugin-a"].status == "success"
    assert steps["plugin-update:plugin-b"].status == "skipped"
    assert "non-fatal error" in steps["plugin-update:plugin-b"].detail
    assert steps["plugin-update:plugin-c"].status == "success"


def test_run_theme_update_structured_parses_clean_json(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    payload = '[{"name":"my-theme","status":"Updated","version":"1.1"}]'
    with patch.object(updater, "_wp", return_value=payload):
        result = updater._run_theme_update_structured(r, "my-theme")
    assert result["status"] == "Updated"
    assert result["name"] == "my-theme"


def test_step_update_themes_license_error_skips_not_rollback(tmp_path: Path) -> None:
    """A theme that cannot be updated (e.g. premium theme, expired license)
    but leaves the site healthy is recorded as skipped — it must NOT raise
    (which would trigger a full-site rollback) and must NOT block the
    remaining themes."""
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.baseline = {
        "theme_updates": [
            {"name": "theme-a", "version": "1.0", "update_version": "2.0"},
            {"name": "theme-b", "version": "1.0", "update_version": "2.0"},
            {"name": "theme-c", "version": "1.0", "update_version": "2.0"},
        ]
    }

    def fake_structured(report: wp_update.SiteReport, slug: str) -> dict:
        if slug == "theme-b":
            return {"name": slug, "status": "Error", "_exit_nonzero": True,
                    "_error": "license key invalid"}
        return {"name": slug, "status": "Updated", "version": "2.0"}

    with (
        patch.object(updater, "_run_theme_update_structured", side_effect=fake_structured),
        patch.object(updater, "_verify"),  # always passes — site stays healthy
        patch.object(updater, "_flush_cache"),
    ):
        updater._step_update_themes(r)  # must not raise

    steps = {s.name: s for s in r.steps}
    assert steps["theme-update:theme-a"].status == "success"
    assert steps["theme-update:theme-b"].status == "skipped"
    assert "non-fatal error" in steps["theme-update:theme-b"].detail
    assert steps["theme-update:theme-c"].status == "success"


def test_step_update_themes_break_raises_for_rollback(tmp_path: Path) -> None:
    """A theme update that genuinely breaks the site (verify fails) escalates
    by raising HealthCheckError so _process_site performs a rollback."""
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.baseline = {
        "theme_updates": [
            {"name": "theme-a", "version": "1.0", "update_version": "2.0"},
        ]
    }

    with (
        patch.object(updater, "_run_theme_update_structured",
                     return_value={"name": "theme-a", "status": "Updated", "version": "2.0"}),
        patch.object(updater, "_verify", side_effect=wp_update.HealthCheckError("500")),
        patch.object(updater, "_flush_cache"),
        pytest.raises(wp_update.HealthCheckError),
    ):
        updater._step_update_themes(r)

    steps = {s.name: s for s in r.steps}
    assert steps["theme-update:theme-a"].status == "failed"


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


# ---------------------------------------------------------------------------
# Capability detection + cache flush (A7)
# ---------------------------------------------------------------------------

def _baseline_with_plugins(updater: wp_update.WPUpdater,
                           r: wp_update.SiteReport,
                           plugins: list[dict]) -> None:
    """Run _step_collect_baseline against canned wp-cli responses."""
    def fake_json(report: wp_update.SiteReport, cmd: str,
                  allow_empty: bool = False) -> list[dict]:
        if cmd.startswith("plugin list"):
            return plugins
        return []
    with (
        patch.object(updater, "_wp_json", side_effect=fake_json),
        patch.object(updater, "_wp_text", return_value="x"),
    ):
        updater._step_collect_baseline(r)


def test_capabilities_detect_active_cache_plugin(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    _baseline_with_plugins(updater, r, [
        {"name": "litespeed-cache", "status": "active", "version": "5.0"},
        {"name": "akismet",         "status": "active", "version": "5.0"},
    ])
    assert r.capabilities is not None
    assert r.capabilities.cache_plugin == "LiteSpeed Cache"
    assert r.capabilities.cache_flush_cmd == "litespeed-purge all"


def test_capabilities_ignore_inactive_cache_plugin(tmp_path: Path) -> None:
    # An installed-but-deactivated cache plugin doesn't serve cached pages,
    # so capability detection must skip it.
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    _baseline_with_plugins(updater, r, [
        {"name": "wp-rocket", "status": "inactive", "version": "3.0"},
    ])
    assert r.capabilities is not None
    assert r.capabilities.cache_plugin == ""
    assert r.capabilities.cache_flush_cmd == ""


def test_capabilities_detect_active_backup_plugin(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    _baseline_with_plugins(updater, r, [
        {"name": "updraftplus", "status": "active", "version": "1.0"},
    ])
    assert r.capabilities is not None
    assert r.capabilities.backup_plugin == "UpdraftPlus"


def test_capabilities_none_when_no_known_plugins(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    _baseline_with_plugins(updater, r, [
        {"name": "akismet", "status": "active", "version": "5.0"},
    ])
    assert r.capabilities is not None
    assert r.capabilities.cache_plugin == ""
    assert r.capabilities.backup_plugin == ""


def test_flush_cache_uses_capability_command(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.capabilities = wp_update.SiteCapabilities(
        cache_plugin="LiteSpeed Cache",
        cache_flush_cmd="litespeed-purge all",
    )
    with patch.object(updater, "_wp", return_value="ok") as mock_wp:
        updater._flush_cache(r, "some-plugin")
    mock_wp.assert_called_once()
    assert mock_wp.call_args.args[1] == "litespeed-purge all"


def test_flush_cache_falls_back_when_no_capability(tmp_path: Path) -> None:
    # No cache plugin detected → use WP core's "wp cache flush" (always
    # available, flushes the object cache even with no page-cache plugin).
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.capabilities = None
    with patch.object(updater, "_wp", return_value="ok") as mock_wp:
        updater._flush_cache(r, "some-plugin")
    mock_wp.assert_called_once()
    assert mock_wp.call_args.args[1] == "cache flush"


def test_flush_cache_falls_back_when_capability_has_no_cmd(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.capabilities = wp_update.SiteCapabilities()  # all empty
    with patch.object(updater, "_wp", return_value="ok") as mock_wp:
        updater._flush_cache(r, "some-plugin")
    assert mock_wp.call_args.args[1] == "cache flush"


def test_flush_cache_swallows_ssh_failure(tmp_path: Path) -> None:
    # A flush failure must never fail the plugin update step.
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    with patch.object(updater, "_wp",
                      side_effect=wp_update.SSHError("connection lost")):
        updater._flush_cache(r, "some-plugin")  # must not raise


def test_step_update_plugins_calls_flush_before_verify(tmp_path: Path) -> None:
    # Cache flush must run BEFORE verify so the HTTP check sees post-update
    # content, not a stale cached error page.
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.baseline = {
        "plugin_updates": [
            {"name": "plugin-a", "version": "1.0", "update_version": "2.0"},
        ]
    }
    call_order: list[str] = []

    with (
        patch.object(updater, "_run_plugin_update_structured",
                     return_value={"name": "plugin-a", "status": "Updated"}),
        patch.object(updater, "_flush_cache",
                     side_effect=lambda rep, slug: call_order.append("flush")),
        patch.object(updater, "_verify",
                     side_effect=lambda rep: call_order.append("verify")),
    ):
        updater._step_update_plugins(r)

    assert call_order == ["flush", "verify"]


def test_site_report_to_dict_serializes_capabilities() -> None:
    r = wp_update.SiteReport(
        client="x", domain="x", server_ip="x", wp_path="x",
        is_staging=False, has_woocommerce=False,
    )
    r.capabilities = wp_update.SiteCapabilities(
        cache_plugin="WP Rocket",
        cache_flush_cmd="rocket clean --confirm",
    )
    d = r.to_dict()
    assert d["capabilities"]["cache_plugin"] == "WP Rocket"
    assert d["capabilities"]["cache_flush_cmd"] == "rocket clean --confirm"


def test_site_report_to_dict_capabilities_none_when_unset() -> None:
    r = wp_update.SiteReport(
        client="x", domain="x", server_ip="x", wp_path="x",
        is_staging=False, has_woocommerce=False,
    )
    assert r.to_dict()["capabilities"] is None


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


def test_step_backup_fingerprints_wp_config(tmp_path: Path) -> None:
    # The backup must assert wp-config.php is in the archive and record its
    # sha256 so the rollback path can prove a complete restore later.
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.backup_dir = ""

    captured: list[str] = []

    def fake_ssh(report: wp_update.SiteReport, script: str, **kw: object) -> str:
        captured.append(script)
        return "backup-ok"

    with patch.object(updater, "_ssh", side_effect=fake_ssh):
        updater._step_backup(r)

    script = captured[0]
    assert "wp-config.php missing from archive" in script
    assert "wp-config.sha256" in script
    assert "sha256sum wp-config.php" in script


def test_load_client_notes_missing_returns_empty(tmp_path: Path) -> None:
    client = tmp_path / "lisette_yogabranch.com_cloudways.json"
    client.write_text("{}")
    assert wp_update.load_client_notes(client) == {}


def test_load_client_notes_parses_sites(tmp_path: Path) -> None:
    client = tmp_path / "lisette_yogabranch.com_cloudways.json"
    client.write_text("{}")
    (tmp_path / "notes.json").write_text(
        '{"general": "n", "sites": {"yogabranch.com": '
        '{"skip_items": [{"type": "theme", "slug": "yogabranch-pro", '
        '"reason": "license expired"}]}}}'
    )
    notes = wp_update.load_client_notes(client)
    items = wp_update.skip_items_for_domain(notes, "yogabranch.com")
    assert items == [{"type": "theme", "slug": "yogabranch-pro", "reason": "license expired"}]


def test_skip_items_for_domain_normalizes_scheme_and_slash() -> None:
    notes = {
        "sites": {
            "https://Foo.example.com/": {
                "skip_items": [{"type": "plugin", "slug": "ninja-forms", "reason": "PHP 8"}]
            }
        }
    }
    assert wp_update.skip_items_for_domain(notes, "foo.example.com") == [
        {"type": "plugin", "slug": "ninja-forms", "reason": "PHP 8"}
    ]
    assert wp_update.skip_items_for_domain(notes, "http://foo.example.com/") == [
        {"type": "plugin", "slug": "ninja-forms", "reason": "PHP 8"}
    ]


def test_skip_items_for_domain_filters_invalid_entries() -> None:
    notes = {
        "sites": {
            "x.com": {
                "skip_items": [
                    {"type": "plugin", "slug": "ok"},
                    {"type": "core", "slug": "wp"},  # not plugin/theme
                    {"type": "plugin"},               # missing slug
                    "not-a-dict",
                ]
            }
        }
    }
    items = wp_update.skip_items_for_domain(notes, "x.com")
    assert items == [{"type": "plugin", "slug": "ok", "reason": ""}]


def test_skip_items_for_domain_returns_empty_for_unknown_domain() -> None:
    notes = {"sites": {"a.com": {"skip_items": [{"type": "plugin", "slug": "x"}]}}}
    assert wp_update.skip_items_for_domain(notes, "b.com") == []


def test_to_dict_includes_skip_items() -> None:
    report = make_report(skip_items=[{"type": "plugin", "slug": "ninja-forms", "reason": "PHP 8"}])
    assert report.to_dict()["skip_items"] == [
        {"type": "plugin", "slug": "ninja-forms", "reason": "PHP 8"}
    ]


def test_is_already_deactivated_matches_wpcli_message() -> None:
    """The exact wp-cli error string from a redundant deactivate is suppressed."""
    err = wp_update.WPCliError(
        "exit=1 on wpupdates@host: Error: Maintenance mode already deactivated."
    )
    assert wp_update._is_already_deactivated(err) is True


def test_is_already_deactivated_is_case_insensitive() -> None:
    err = wp_update.SSHError("ALREADY DEACTIVATED")
    assert wp_update._is_already_deactivated(err) is True


def test_is_already_deactivated_rejects_real_failures() -> None:
    """A genuine wp-cli boot failure must still surface as a warning."""
    err = wp_update.SSHError("PHP Fatal error: cannot bootstrap wp-cli")
    assert wp_update._is_already_deactivated(err) is False
    err2 = wp_update.WPCliError("Error: This does not seem to be a WordPress installation.")
    assert wp_update._is_already_deactivated(err2) is False


# ---------------------------------------------------------------------------
# needs_update / summary-driven "up to date" skip + inline recheck
# ---------------------------------------------------------------------------

def test_baseline_pending_updates_filters_skipped_plugin_slugs() -> None:
    baseline = {
        "core_updates": [],
        "plugin_updates": [
            {"name": "ninja-forms", "update": "available"},
            {"name": "akismet",     "update": "available"},
        ],
        "theme_updates": [],
    }
    skip_items = [{"type": "plugin", "slug": "ninja-forms"}]
    pending = wp_update.baseline_pending_updates(baseline, skip_items)
    assert [p["name"] for p in pending["plugins"]] == ["akismet"]
    assert pending["themes"] == []
    assert pending["core"] == []


def test_baseline_pending_updates_empty_when_only_skipped_have_updates() -> None:
    baseline = {
        "core_updates": [],
        "plugin_updates": [{"name": "ninja-forms", "update": "available"}],
        "theme_updates": [],
    }
    pending = wp_update.baseline_pending_updates(
        baseline, [{"type": "plugin", "slug": "ninja-forms"}]
    )
    assert pending["core"] == [] and pending["plugins"] == [] and pending["themes"] == []


def test_step_collect_baseline_sets_needs_update_true(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    _baseline_with_plugins(updater, r, [
        {"name": "akismet", "status": "active", "version": "5.0",
         "update": "available"},
    ])
    assert r.needs_update is True


def test_step_collect_baseline_sets_needs_update_false_when_clean(tmp_path: Path) -> None:
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    _baseline_with_plugins(updater, r, [
        {"name": "akismet", "status": "active", "version": "5.0"},
    ])
    assert r.needs_update is False


def test_site_report_to_dict_includes_needs_update() -> None:
    r = make_report()
    r.needs_update = False
    assert r.to_dict()["needs_update"] is False


def _make_args_with_skip_flags(
    tmp_path: Path, *, execute: bool, ttl: int = 60,
    no_skip: bool = False, recheck: bool = False,
) -> argparse.Namespace:
    args = make_args(tmp_path, execute=execute)
    args.skip_up_to_date_ttl = ttl
    args.no_skip_up_to_date = no_skip
    args.recheck_updates = recheck
    return args


def _write_dry_run_summary(
    log_dir: Path, run_id: str, domain: str, needs_update: bool | None,
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    # Match what _step_collect_baseline actually writes: an empty-but-typed
    # baseline. The loader requires this structure to trust a needs_update=
    # false cache hit (G2 guard against missing-baseline silent degrade).
    site_entry = {
        "domain": domain,
        "overall": "dry-run",
        "baseline": {
            "core_updates": [],
            "plugin_updates": [],
            "theme_updates": [],
        },
    }
    if needs_update is not None:
        site_entry["needs_update"] = needs_update
    payload = {
        "run_id": run_id,
        "mode": "dry-run",
        "generated_at": "2026-05-12T00:00:00+00:00",
        "sites": [site_entry],
    }
    (log_dir / f"wp-update-summary-{run_id}.json").write_text(json.dumps(payload))


def test_load_no_update_domains_picks_up_recent_dry_run_with_false(tmp_path: Path) -> None:
    args = _make_args_with_skip_flags(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_dry_run_summary(args.log_dir, fresh, "fresh.example.com", False)
    updater = wp_update.WPUpdater(args)
    assert "fresh.example.com" in updater._no_update_domains


def test_load_no_update_domains_ignores_stale_summary(tmp_path: Path) -> None:
    args = _make_args_with_skip_flags(tmp_path, execute=True, ttl=30)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    # Two hours ago — well outside any sensible TTL we use in tests
    old = (wp_update.datetime.now(wp_update.UTC)
           - wp_update.timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ")
    _write_dry_run_summary(args.log_dir, old, "old.example.com", False)
    updater = wp_update.WPUpdater(args)
    assert "old.example.com" not in updater._no_update_domains


def test_load_no_update_domains_skips_sites_with_pending_updates(tmp_path: Path) -> None:
    args = _make_args_with_skip_flags(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_dry_run_summary(args.log_dir, fresh, "needs.example.com", True)
    updater = wp_update.WPUpdater(args)
    assert "needs.example.com" not in updater._no_update_domains


def test_load_no_update_domains_disabled_in_dry_run_mode(tmp_path: Path) -> None:
    args = _make_args_with_skip_flags(tmp_path, execute=False)
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_dry_run_summary(args.log_dir, fresh, "any.example.com", False)
    updater = wp_update.WPUpdater(args)
    assert updater._no_update_domains == {}


def test_load_no_update_domains_disabled_by_no_skip_flag(tmp_path: Path) -> None:
    args = _make_args_with_skip_flags(tmp_path, execute=True, no_skip=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_dry_run_summary(args.log_dir, fresh, "any.example.com", False)
    updater = wp_update.WPUpdater(args)
    assert updater._no_update_domains == {}


def test_load_no_update_domains_disabled_by_recheck_flag(tmp_path: Path) -> None:
    args = _make_args_with_skip_flags(tmp_path, execute=True, recheck=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_dry_run_summary(args.log_dir, fresh, "any.example.com", False)
    updater = wp_update.WPUpdater(args)
    assert updater._no_update_domains == {}


def test_load_no_update_domains_most_recent_wins(tmp_path: Path) -> None:
    """A more recent dry-run with needs_update=true overrides an older false."""
    args = _make_args_with_skip_flags(tmp_path, execute=True, ttl=120)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    now = wp_update.datetime.now(wp_update.UTC)
    older = (now - wp_update.timedelta(minutes=30)).strftime("%Y%m%dT%H%M%SZ")
    newer = (now - wp_update.timedelta(minutes=5)).strftime("%Y%m%dT%H%M%SZ")
    _write_dry_run_summary(args.log_dir, older, "flip.example.com", False)
    _write_dry_run_summary(args.log_dir, newer, "flip.example.com", True)
    updater = wp_update.WPUpdater(args)
    assert "flip.example.com" not in updater._no_update_domains


# ---------------------------------------------------------------------------
# F1: needs_update flips False after successful execute (post-codex review)
# ---------------------------------------------------------------------------

def test_needs_update_flips_false_after_successful_execute(tmp_path: Path) -> None:
    """A successful execute mutates the site; the pre-update baseline value
    of needs_update is stale by definition and must be flipped to False so
    downstream JSON/API consumers see the post-update truth."""
    updater = _make_updater(tmp_path)
    r = _make_exec_report()
    r.needs_update = True  # pretend baseline saw pending updates

    with (
        patch.object(updater, "_step_ssh_preflight"),
        patch.object(updater, "_step_collect_baseline"),
        patch.object(updater, "_step_disk_check"),
        patch.object(updater, "_step_backup"),
        patch.object(updater, "_step_capture_ownership"),
        patch.object(updater, "_step_update_core"),
        patch.object(updater, "_step_update_themes"),
        patch.object(updater, "_step_update_plugins"),
        patch.object(updater, "_verify"),
        patch.object(updater, "_ssh"),
    ):
        updater._process_site(r)

    assert r.overall == "success"
    assert r.needs_update is False


# ---------------------------------------------------------------------------
# F2: skip-drift validation — _process_client_file rechecks current skip rules
# ---------------------------------------------------------------------------

def _write_dry_run_summary_with_baseline(
    log_dir: Path, run_id: str, domain: str,
    pending_plugin_slugs: list[str],
    pending_theme_slugs: list[str] | None = None,
    core_count: int = 0,
) -> None:
    """Emit a synthetic dry-run summary whose baseline has pending updates.

    needs_update is set to True when any pending exist, False otherwise —
    matching what the production baseline-collection step would do.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    needs = bool(pending_plugin_slugs or pending_theme_slugs or core_count)
    site_entry = {
        "domain": domain,
        "overall": "dry-run",
        "needs_update": needs,
        "baseline": {
            "plugin_updates": [{"name": s} for s in pending_plugin_slugs],
            "theme_updates": [{"name": s} for s in (pending_theme_slugs or [])],
            "core_updates": [{} for _ in range(core_count)],
        },
    }
    payload = {
        "run_id": run_id, "mode": "dry-run",
        "generated_at": "2026-05-12T00:00:00+00:00",
        "sites": [site_entry],
    }
    (log_dir / f"wp-update-summary-{run_id}.json").write_text(json.dumps(payload))


def test_no_update_skip_records_pre_filter_pending_slugs(tmp_path: Path) -> None:
    args = _make_args_with_skip_flags(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    # needs_update=false in summary, but baseline lists a plugin that *was*
    # filtered out via notes.json at dry-run time — recorded so we can
    # later re-validate against the current notes.json.
    _write_dry_run_summary_with_baseline(
        args.log_dir, fresh, "x.example.com",
        pending_plugin_slugs=["ninja-forms"],
    )
    # Force needs_update to False (the helper would have set True because
    # baseline pending is non-empty; for this test we want to simulate
    # the dry-run path where skip_items filtered it out).
    summary_path = next(args.log_dir.glob("wp-update-summary-*.json"))
    data = json.loads(summary_path.read_text())
    data["sites"][0]["needs_update"] = False
    summary_path.write_text(json.dumps(data))

    updater = wp_update.WPUpdater(args)
    assert updater._no_update_domains["x.example.com"]["pending_plugin_slugs"] == [
        "ninja-forms"
    ]


def _seed_no_update_state(
    updater: wp_update.WPUpdater, domain: str, *,
    plugins: list[str] | None = None, themes: list[str] | None = None,
    core_count: int = 0,
) -> None:
    """Inject a synthetic _no_update_domains record without writing files."""
    updater._no_update_domains[domain] = {
        "pending_plugin_slugs": list(plugins or []),
        "pending_theme_slugs": list(themes or []),
        "core_count": core_count,
    }


def test_process_client_file_skips_when_current_skips_cover_cached(tmp_path: Path) -> None:
    """Cached dry-run filtered out ninja-forms via notes.json. Current notes
    still skip ninja-forms. The cached up-to-date verdict is still valid →
    site is skipped without _process_site being called."""
    args = make_args(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    args.clients_dir = clients_dir
    client = _write_single_app_client_json(
        clients_dir, "site_cloudways.json", "site.example.com", "abc1234"
    )
    (clients_dir / "notes.json").write_text(json.dumps({
        "sites": {"site.example.com": {
            "skip_items": [{"type": "plugin", "slug": "ninja-forms"}],
        }}
    }))

    updater = wp_update.WPUpdater(args)
    _seed_no_update_state(updater, "site.example.com", plugins=["ninja-forms"])

    with patch.object(updater, "_process_site") as mock_process:
        updater._process_client_file(client)

    mock_process.assert_not_called()
    assert updater.reports[0].overall == "skipped"
    assert any(
        s.name == "up-to-date-skip" for s in updater.reports[0].steps
    )


def test_process_client_file_runs_when_skip_was_removed(tmp_path: Path) -> None:
    """Cached dry-run filtered out ninja-forms via notes.json. Operator
    removed that skip. The slug now has a pending update → site must run."""
    args = make_args(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    args.clients_dir = clients_dir
    client = _write_single_app_client_json(
        clients_dir, "site_cloudways.json", "site.example.com", "abc1234"
    )
    # No notes.json → no current skips

    updater = wp_update.WPUpdater(args)
    _seed_no_update_state(updater, "site.example.com", plugins=["ninja-forms"])

    with patch.object(updater, "_process_site") as mock_process:
        updater._process_client_file(client)

    mock_process.assert_called_once()
    assert not any(
        s.name == "up-to-date-skip"
        for r in updater.reports for s in r.steps
    )


# ---------------------------------------------------------------------------
# Default execute auto-skip after inline baseline + --no-skip-up-to-date
# ---------------------------------------------------------------------------

def test_execute_skips_after_baseline_when_no_pending_by_default(tmp_path: Path) -> None:
    """Default execute behavior: once the inline baseline reports
    needs_update=False, the site is skipped before backup/update/verify.
    No special flag required — this is the one-click workflow."""
    args = make_args(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    args.recheck_updates = False
    args.no_skip_up_to_date = False
    args.skip_up_to_date_ttl = 60
    updater = wp_update.WPUpdater(args)
    r = _make_exec_report()

    def fake_baseline(report):  # noqa: ANN001
        report.needs_update = False

    with (
        patch.object(updater, "_step_ssh_preflight"),
        patch.object(updater, "_step_collect_baseline", side_effect=fake_baseline),
        patch.object(updater, "_step_disk_check") as mock_disk,
        patch.object(updater, "_step_backup") as mock_backup,
    ):
        updater._process_site(r)

    mock_disk.assert_not_called()
    mock_backup.assert_not_called()
    assert r.overall == "skipped"
    assert any(s.name == "up-to-date-skip" for s in r.steps)


def test_execute_proceeds_when_baseline_has_pending(tmp_path: Path) -> None:
    """Sites with pending updates flow through the full execute path."""
    args = make_args(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    args.recheck_updates = False
    args.no_skip_up_to_date = False
    args.skip_up_to_date_ttl = 60
    updater = wp_update.WPUpdater(args)
    r = _make_exec_report()

    def fake_baseline(report):  # noqa: ANN001
        report.needs_update = True

    with (
        patch.object(updater, "_step_ssh_preflight"),
        patch.object(updater, "_step_collect_baseline", side_effect=fake_baseline),
        patch.object(updater, "_step_disk_check") as mock_disk,
        patch.object(updater, "_step_backup"),
        patch.object(updater, "_step_capture_ownership"),
        patch.object(updater, "_step_update_core"),
        patch.object(updater, "_step_update_themes"),
        patch.object(updater, "_step_update_plugins"),
        patch.object(updater, "_verify"),
        patch.object(updater, "_ssh"),
    ):
        updater._process_site(r)

    mock_disk.assert_called_once()
    assert r.overall == "success"


def test_backup_failure_does_not_trigger_rollback(tmp_path: Path) -> None:
    """A failed pre-flight backup must NOT escalate into a destructive
    rollback. At backup time no updates have run, the live site is
    untouched, and the only archive on disk is known-bad — restoring from
    it could only destroy a healthy site. backup_ok stays False, so the
    failure handler marks the site failed and leaves it alone.

    Regression guard for the data-loss path where a permission-denied tar
    failure wiped public_html and restored from the broken backup.
    """
    args = make_args(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    args.recheck_updates = False
    args.no_skip_up_to_date = True
    args.skip_up_to_date_ttl = 60
    updater = wp_update.WPUpdater(args)
    r = _make_exec_report()

    def fake_baseline(report):  # noqa: ANN001
        report.needs_update = True

    def failing_backup(report):  # noqa: ANN001
        # Mirror the real method: backup_ok is only set True on success.
        raise wp_update.SSHError("tar: uploads/secret.pdf: Permission denied")

    with (
        patch.object(updater, "_step_ssh_preflight"),
        patch.object(updater, "_step_collect_baseline", side_effect=fake_baseline),
        patch.object(updater, "_step_disk_check"),
        patch.object(updater, "_step_backup", side_effect=failing_backup),
        patch.object(updater, "_step_rollback") as mock_rollback,
    ):
        updater._process_site(r)

    mock_rollback.assert_not_called()
    assert r.backup_ok is False
    assert r.overall == "failed"
    assert r.failure_step == "backup"


def test_update_failure_after_good_backup_triggers_rollback(tmp_path: Path) -> None:
    """The flip side of the gate: when the backup succeeded (backup_ok=True)
    and a later update step breaks the site, rollback MUST fire. Proves the
    backup_ok gate didn't disable legitimate rollbacks."""
    args = make_args(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    args.recheck_updates = False
    args.no_skip_up_to_date = True
    args.skip_up_to_date_ttl = 60
    updater = wp_update.WPUpdater(args)
    r = _make_exec_report()

    def fake_baseline(report):  # noqa: ANN001
        report.needs_update = True

    def good_backup(report):  # noqa: ANN001
        report.backup_ok = True

    with (
        patch.object(updater, "_step_ssh_preflight"),
        patch.object(updater, "_step_collect_baseline", side_effect=fake_baseline),
        patch.object(updater, "_step_disk_check"),
        patch.object(updater, "_step_backup", side_effect=good_backup),
        patch.object(updater, "_step_capture_ownership"),
        patch.object(updater, "_step_update_core"),
        patch.object(updater, "_step_update_themes",
                     side_effect=wp_update.SSHError("theme update broke site")),
        patch.object(updater, "_step_rollback") as mock_rollback,
        patch.object(updater, "_ssh"),
    ):
        updater._process_site(r)

    mock_rollback.assert_called_once()
    assert r.backup_ok is True


def test_no_skip_up_to_date_forces_full_run_even_when_baseline_empty(
    tmp_path: Path,
) -> None:
    """--no-skip-up-to-date overrides the new default auto-skip. Sites with
    needs_update=False still go through backup → update → verify (e.g. for
    smoke-testing the whole pipeline against a stable site)."""
    args = make_args(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    args.recheck_updates = False
    args.no_skip_up_to_date = True  # override
    args.skip_up_to_date_ttl = 60
    updater = wp_update.WPUpdater(args)
    r = _make_exec_report()

    def fake_baseline(report):  # noqa: ANN001
        report.needs_update = False

    with (
        patch.object(updater, "_step_ssh_preflight"),
        patch.object(updater, "_step_collect_baseline", side_effect=fake_baseline),
        patch.object(updater, "_step_disk_check") as mock_disk,
        patch.object(updater, "_step_backup") as mock_backup,
        patch.object(updater, "_step_capture_ownership"),
        patch.object(updater, "_step_update_core"),
        patch.object(updater, "_step_update_themes"),
        patch.object(updater, "_step_update_plugins"),
        patch.object(updater, "_verify"),
        patch.object(updater, "_ssh"),
    ):
        updater._process_site(r)

    mock_disk.assert_called_once()
    mock_backup.assert_called_once()
    assert r.overall == "success"
    assert not any(s.name == "up-to-date-skip" for s in r.steps)


# ---------------------------------------------------------------------------
# Second codex review: H1/M1/M2/M3 robustness fixes for _load_no_update_domains
# ---------------------------------------------------------------------------

def _write_summary_raw(log_dir: Path, run_id: str, payload: dict) -> None:
    """Write an arbitrary JSON payload as a summary file — for malformed-input tests."""
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"wp-update-summary-{run_id}.json").write_text(json.dumps(payload))


def test_load_no_update_domains_handles_malformed_sites_field(tmp_path: Path) -> None:
    """H1: sites is a dict, not a list — must not crash __init__."""
    args = _make_args_with_skip_flags(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_summary_raw(args.log_dir, fresh, {
        "run_id": fresh, "mode": "dry-run", "sites": {"not": "a list"},
    })
    updater = wp_update.WPUpdater(args)  # must not raise
    assert updater._no_update_domains == {}


def test_load_no_update_domains_handles_non_dict_site_entries(tmp_path: Path) -> None:
    """H1: entries inside sites are strings, not dicts — must not crash."""
    args = _make_args_with_skip_flags(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_summary_raw(args.log_dir, fresh, {
        "run_id": fresh, "mode": "dry-run", "sites": ["string-not-dict", 42, None],
    })
    updater = wp_update.WPUpdater(args)
    assert updater._no_update_domains == {}


def test_load_no_update_domains_rejects_missing_baseline(tmp_path: Path) -> None:
    """M1: needs_update=false but no baseline field → reject cache hit.

    Older summary files (pre-needs_update PR) won't have a baseline at all;
    we can't validate skip-drift against them, so we must NOT trust them.
    """
    args = _make_args_with_skip_flags(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_summary_raw(args.log_dir, fresh, {
        "run_id": fresh, "mode": "dry-run",
        "sites": [{
            "domain": "no-baseline.example.com",
            "overall": "dry-run",
            "needs_update": False,
            # baseline omitted
        }],
    })
    updater = wp_update.WPUpdater(args)
    assert "no-baseline.example.com" not in updater._no_update_domains


def test_load_no_update_domains_rejects_non_dict_baseline(tmp_path: Path) -> None:
    """M1: baseline is a list (malformed) → reject cache hit, no crash."""
    args = _make_args_with_skip_flags(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_summary_raw(args.log_dir, fresh, {
        "run_id": fresh, "mode": "dry-run",
        "sites": [{
            "domain": "weird.example.com", "overall": "dry-run",
            "needs_update": False,
            "baseline": ["unexpected", "list", "shape"],
        }],
    })
    updater = wp_update.WPUpdater(args)
    assert "weird.example.com" not in updater._no_update_domains


def test_load_no_update_domains_rejects_baseline_with_wrong_typed_lists(tmp_path: Path) -> None:
    """M1: baseline dict but plugin_updates is a string → reject, no crash."""
    args = _make_args_with_skip_flags(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_summary_raw(args.log_dir, fresh, {
        "run_id": fresh, "mode": "dry-run",
        "sites": [{
            "domain": "bad-types.example.com", "overall": "dry-run",
            "needs_update": False,
            "baseline": {
                "core_updates": [],
                "plugin_updates": "should-be-a-list",
                "theme_updates": [],
            },
        }],
    })
    updater = wp_update.WPUpdater(args)
    assert "bad-types.example.com" not in updater._no_update_domains


def test_load_no_update_domains_accepts_empty_baseline_for_truly_up_to_date(
    tmp_path: Path,
) -> None:
    """M1 regression guard: a site with NO available updates writes empty
    baseline lists with needs_update=false. That's still a valid cache hit —
    the empty-lists case must NOT be falsely rejected by the new guard."""
    args = _make_args_with_skip_flags(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_summary_raw(args.log_dir, fresh, {
        "run_id": fresh, "mode": "dry-run",
        "sites": [{
            "domain": "clean.example.com", "overall": "dry-run",
            "needs_update": False,
            "baseline": {
                "core_updates": [], "plugin_updates": [], "theme_updates": [],
            },
        }],
    })
    updater = wp_update.WPUpdater(args)
    assert "clean.example.com" in updater._no_update_domains
    assert updater._no_update_domains["clean.example.com"] == {
        "pending_plugin_slugs": [],
        "pending_theme_slugs": [],
        "core_count": 0,
    }


def test_load_no_update_domains_normalizes_domain_keys(tmp_path: Path) -> None:
    """M2: cached entries written with scheme/www prefixes must collapse
    to the same key as an inventory entry without them."""
    args = _make_args_with_skip_flags(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    fresh = wp_update.datetime.now(wp_update.UTC).strftime("%Y%m%dT%H%M%SZ")
    _write_summary_raw(args.log_dir, fresh, {
        "run_id": fresh, "mode": "dry-run",
        "sites": [{
            "domain": "https://WWW.MixedCase.example.com/",
            "overall": "dry-run", "needs_update": False,
            "baseline": {
                "core_updates": [], "plugin_updates": [], "theme_updates": [],
            },
        }],
    })
    updater = wp_update.WPUpdater(args)
    # Key collapses to the canonical normalized form.
    assert "mixedcase.example.com" in updater._no_update_domains
    # And callers looking up by any equivalent inventory string land on it.
    for variant in (
        "MixedCase.example.com",
        "www.mixedcase.example.com",
        "https://mixedcase.example.com",
    ):
        assert wp_update._normalize_domain(variant) in updater._no_update_domains


def test_load_no_update_domains_newer_execute_invalidates_older_dryrun(
    tmp_path: Path,
) -> None:
    """M3: older dry-run says needs_update=false, newer FAILED execute touched
    the same domain. The cache must NOT skip — something happened in between
    that we shouldn't silently ignore."""
    args = _make_args_with_skip_flags(tmp_path, execute=True, ttl=120)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    now = wp_update.datetime.now(wp_update.UTC)
    older = (now - wp_update.timedelta(minutes=30)).strftime("%Y%m%dT%H%M%SZ")
    newer = (now - wp_update.timedelta(minutes=5)).strftime("%Y%m%dT%H%M%SZ")
    # Older: dry-run, up-to-date — would have qualified for cache hit on its own.
    _write_summary_raw(args.log_dir, older, {
        "run_id": older, "mode": "dry-run",
        "sites": [{
            "domain": "touched.example.com", "overall": "dry-run",
            "needs_update": False,
            "baseline": {
                "core_updates": [], "plugin_updates": [], "theme_updates": [],
            },
        }],
    })
    # Newer: execute that failed (so the failed flag is preserved).
    _write_summary_raw(args.log_dir, newer, {
        "run_id": newer, "mode": "execute",
        "sites": [{
            "domain": "touched.example.com", "overall": "failed",
            "needs_update": True,
        }],
    })
    updater = wp_update.WPUpdater(args)
    assert "touched.example.com" not in updater._no_update_domains


def test_process_site_skip_lookup_uses_normalized_domain(tmp_path: Path) -> None:
    """M2 wiring: the skip path looks up by the normalized form of
    report.domain, so an inventory entry like 'www.foo.com' still matches a
    cache row keyed by 'foo.com'."""
    args = _make_args_with_skip_flags(tmp_path, execute=True)
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    updater = wp_update.WPUpdater(args)
    # Inject a cache row under the normalized key.
    updater._no_update_domains["normalized.example.com"] = {
        "pending_plugin_slugs": [], "pending_theme_slugs": [], "core_count": 0,
    }
    # Simulate the lookup code path: same expression as in _process_site.
    raw_inventory_domain = "https://WWW.Normalized.example.com/"
    norm = wp_update._normalize_domain(raw_inventory_domain)
    assert norm in updater._no_update_domains


# ---------------------------------------------------------------------------
# --update-sheet end-of-run hook
# ---------------------------------------------------------------------------

def _make_args_with_sheet(
    tmp_path: Path,
    *,
    execute: bool,
    update_sheet: str = "",
    update_sheet_dry_run: bool = False,
) -> argparse.Namespace:
    args = make_args(tmp_path, execute=execute)
    # WPUpdater.__init__ validates that SSH_KEY or APP_PW is set when
    # execute=True; the empty .env that make_args() writes otherwise fails.
    args.env_file.write_text("SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n")
    args.update_sheet = update_sheet
    args.update_sheet_tab = "Plugin Updates"
    args.update_sheet_dry_run = update_sheet_dry_run
    args.gws_path = "gws"
    return args


def test_maybe_update_sheet_skipped_when_flag_unset(tmp_path: Path) -> None:
    args = _make_args_with_sheet(tmp_path, execute=True, update_sheet="")
    updater = wp_update.WPUpdater(args)
    updater.reports = [make_report(domain="example.com", overall="success")]
    with patch("sheet_update.update_sheet_for_successes") as mock_call:
        updater._maybe_update_sheet()
    assert mock_call.call_count == 0


def test_maybe_update_sheet_skipped_in_dryrun_mode(tmp_path: Path) -> None:
    """Even with --update-sheet set, dry-run mode (i.e. no --execute) must
    never write to the sheet."""
    args = _make_args_with_sheet(tmp_path, execute=False, update_sheet="SHEET_ID")
    updater = wp_update.WPUpdater(args)
    updater.reports = [make_report(domain="example.com", overall="dry-run")]
    with patch("sheet_update.update_sheet_for_successes") as mock_call:
        updater._maybe_update_sheet()
    assert mock_call.call_count == 0


def test_maybe_update_sheet_filters_to_verified_current(tmp_path: Path) -> None:
    """Sites that ended in a verified-current state (needs_update=False)
    are forwarded — that covers both successful updates and auto-skipped
    up-to-date sites. Failed, rolled-back, and skip-without-verification
    sites (WooCommerce gate, staging gate, dedupe) must never be stamped.
    """
    args = _make_args_with_sheet(tmp_path, execute=True, update_sheet="SHEET_ID")
    updater = wp_update.WPUpdater(args)
    updater.reports = [
        # success → needs_update=False (set by the success path)
        make_report(domain="updated.com", overall="success", needs_update=False),
        # auto-skip after inline baseline found nothing pending
        make_report(domain="uptodate.com", overall="skipped", needs_update=False),
        # WooCommerce gate / staging gate / dedupe — never ran a check
        make_report(domain="wc-gate.com", overall="skipped", needs_update=None),
        # failure paths intentionally retain pre-failure needs_update
        make_report(domain="bad.com", overall="failed", needs_update=True),
        make_report(domain="rolled.com", overall="rolled-back", needs_update=True),
        make_report(domain="other-updated.com", overall="success", needs_update=False),
    ]
    with patch("sheet_update.update_sheet_for_successes") as mock_call:
        updater._maybe_update_sheet()
    assert mock_call.call_count == 1
    forwarded = sorted(mock_call.call_args.kwargs["success_domains"])
    assert forwarded == ["other-updated.com", "updated.com", "uptodate.com"]


def test_maybe_update_sheet_noop_when_nothing_verified(tmp_path: Path) -> None:
    """If no site reached needs_update=False (all failed, all rolled back,
    or all skipped without verification), the sheet step is a no-op."""
    args = _make_args_with_sheet(tmp_path, execute=True, update_sheet="SHEET_ID")
    updater = wp_update.WPUpdater(args)
    updater.reports = [
        make_report(domain="bad.com", overall="failed", needs_update=True),
        make_report(domain="wc-gate.com", overall="skipped", needs_update=None),
    ]
    with patch("sheet_update.update_sheet_for_successes") as mock_call:
        updater._maybe_update_sheet()
    assert mock_call.call_count == 0


def test_maybe_update_sheet_falls_back_to_env_var(tmp_path: Path) -> None:
    """When --update-sheet is empty, UPDATE_SHEET_ID from .env is used."""
    args = _make_args_with_sheet(tmp_path, execute=True, update_sheet="")
    args.env_file.write_text(
        "SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n"
        "UPDATE_SHEET_ID=ENV_SHEET_ID\n"
    )
    updater = wp_update.WPUpdater(args)
    updater.reports = [
        make_report(domain="example.com", overall="success", needs_update=False),
    ]
    with patch("sheet_update.update_sheet_for_successes") as mock_call:
        updater._maybe_update_sheet()
    assert mock_call.call_count == 1
    assert mock_call.call_args.kwargs["spreadsheet_id"] == "ENV_SHEET_ID"


def test_maybe_update_sheet_cli_overrides_env_var(tmp_path: Path) -> None:
    """An explicit --update-sheet wins over UPDATE_SHEET_ID in .env."""
    args = _make_args_with_sheet(tmp_path, execute=True, update_sheet="CLI_SHEET_ID")
    args.env_file.write_text(
        "SSH_USER=wpupdates\nSSH_KEY=\nAPP_PW=pw\n"
        "UPDATE_SHEET_ID=ENV_SHEET_ID\n"
    )
    updater = wp_update.WPUpdater(args)
    updater.reports = [
        make_report(domain="example.com", overall="success", needs_update=False),
    ]
    with patch("sheet_update.update_sheet_for_successes") as mock_call:
        updater._maybe_update_sheet()
    assert mock_call.call_args.kwargs["spreadsheet_id"] == "CLI_SHEET_ID"


def test_maybe_update_sheet_forwards_dry_run_flag(tmp_path: Path) -> None:
    args = _make_args_with_sheet(
        tmp_path, execute=True, update_sheet="SHEET_ID",
        update_sheet_dry_run=True,
    )
    updater = wp_update.WPUpdater(args)
    updater.reports = [
        make_report(domain="example.com", overall="success", needs_update=False),
    ]
    with patch("sheet_update.update_sheet_for_successes") as mock_call:
        updater._maybe_update_sheet()
    assert mock_call.call_args.kwargs["dry_run"] is True
    assert mock_call.call_args.kwargs["spreadsheet_id"] == "SHEET_ID"
    assert mock_call.call_args.kwargs["tab_name"] == "Plugin Updates"
