from __future__ import annotations

import argparse
from pathlib import Path

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

    def __enter__(self) -> "DummyResponse":
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
