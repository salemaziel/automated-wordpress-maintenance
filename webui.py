#!/usr/bin/env python3
"""Authenticated web UI for running wp_update.py locally or over SSH."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import db

ROOT = Path(__file__).resolve().parent
CLIENTS_DIR = ROOT / "clients"
SCRIPT_PATH = ROOT / "wp_update.py"
KEYS_DIR = ROOT / ".webui-keys"
LOGS_DIR = ROOT / "logs"
DB_PATH = ROOT / "db" / "wpmaint.db"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
SESSION_COOKIE = "wpmaint_session"
CSRF_COOKIE = "csrf_token"
SESSION_TTL_SECONDS = 12 * 60 * 60
MAX_BODY_BYTES = 1024 * 1024
LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 60

_RUN_ID_PATTERN = re.compile(r"WordPress Maintenance Run\s*\|\s*ID:\s*(\S+)")
_SUMMARY_PATTERN = re.compile(r"Summary written to (.+\.json)\s*$")

DB_CONN: sqlite3.Connection | None = None


@dataclass(frozen=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    username: str = "admin"
    password: str = ""
    secret: str = ""
    remote_host: str = ""
    remote_user: str = ""
    remote_port: int = 22
    remote_repo_path: str = ""
    remote_identity_file: str = ""
    remote_python: str = "python3"

    @classmethod
    def from_env(cls) -> Settings:
        secret = os.environ.get("WEBUI_SECRET") or secrets.token_urlsafe(32)
        return cls(
            host=os.environ.get("WEBUI_HOST", DEFAULT_HOST),
            port=int(os.environ.get("WEBUI_PORT", str(DEFAULT_PORT))),
            username=os.environ.get("WEBUI_USERNAME", "admin"),
            password=os.environ.get("WEBUI_PASSWORD", ""),
            secret=secret,
            remote_host=os.environ.get("WEBUI_REMOTE_HOST", ""),
            remote_user=os.environ.get("WEBUI_REMOTE_USER", ""),
            remote_port=int(os.environ.get("WEBUI_REMOTE_PORT", "22")),
            remote_repo_path=os.environ.get("WEBUI_REMOTE_REPO_PATH", ""),
            remote_identity_file=os.environ.get("WEBUI_REMOTE_IDENTITY_FILE", ""),
            remote_python=os.environ.get("WEBUI_REMOTE_PYTHON", "python3"),
        )


@dataclass
class RunRecord:
    run_id: str
    command: list[str]
    mode: str
    target: str
    client_file: str = ""
    include_woo: bool = False
    started_by: str = ""
    started_at: float = field(default_factory=time.time)
    status: str = "running"
    exit_code: int | None = None
    wp_run_id: str | None = None
    summary_path: str | None = None
    log_path: str | None = None
    lines: list[str] = field(default_factory=list)
    listeners: list[queue.Queue[str | None]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    proc: subprocess.Popen[str] | None = None
    cancel_requested: bool = False
    cancelled_at: float | None = None

    def publish(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            listeners = list(self.listeners)
        for listener in listeners:
            listener.put(line)

    def mark_finished(self, exit_code: int) -> None:
        # Set terminal status WITHOUT notifying listeners. Caller must
        # invoke notify_done() after DB finalization so the SSE 'done'
        # event never beats the persisted summary.
        with self.lock:
            self.exit_code = exit_code
            if self.cancel_requested:
                self.status = "cancelled"
            else:
                self.status = "success" if exit_code == 0 else "failed"

    def notify_done(self) -> None:
        with self.lock:
            listeners = list(self.listeners)
        for listener in listeners:
            listener.put(None)

    def finish(self, exit_code: int) -> None:
        # Compatibility shim for early-failure paths that have no DB to
        # finalize. New code should prefer mark_finished + notify_done.
        self.mark_finished(exit_code)
        self.notify_done()


RUNS: dict[str, RunRecord] = {}
RUNS_LOCK = threading.Lock()


def init_db(path: Path | None = None) -> sqlite3.Connection:
    """Open the persistent DB, sweep orphaned 'running' rows, and retry
    any pending summary ingests left behind by a prior crash."""
    global DB_CONN
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    DB_CONN = db.open_db(target)
    with RUNS_LOCK:
        alive = set(RUNS.keys())
    db.sweep_orphan_running(DB_CONN, alive_ids=alive)
    db.reconcile_pending_ingests(DB_CONN, log_dir=LOGS_DIR)
    return DB_CONN


def sign_session(
    username: str, secret: str, now: int | None = None, csrf: str = ""
) -> str:
    issued = int(now if now is not None else time.time())
    body: dict[str, Any] = {"u": username, "iat": issued}
    if csrf:
        body["c"] = csrf
    payload = json.dumps(body, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _decode_session(token: str, secret: str, now: int | None = None) -> dict[str, Any] | None:
    if "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, json.JSONDecodeError):
        return None
    issued = int(payload.get("iat", 0))
    current = int(now if now is not None else time.time())
    if current - issued > SESSION_TTL_SECONDS:
        return None
    return payload if isinstance(payload, dict) else None


def verify_session(token: str, secret: str, now: int | None = None) -> str | None:
    payload = _decode_session(token, secret, now)
    if payload is None:
        return None
    username = payload.get("u")
    return username if isinstance(username, str) and username else None


def session_csrf(token: str, secret: str, now: int | None = None) -> str | None:
    payload = _decode_session(token, secret, now)
    if payload is None:
        return None
    csrf = payload.get("c")
    return csrf if isinstance(csrf, str) and csrf else None


def slugify(value: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in value.lower())
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or "client"


def validate_client_doc(doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    provider = str(doc.get("hosting_provider") or "Cloudways").strip() or "Cloudways"
    if not doc.get("client_name"):
        errors.append("client_name is required")
    if not doc.get("server_ip_address"):
        errors.append("server_ip_address is required")
    master = doc.get("master_credentials")
    if not isinstance(master, dict):
        errors.append("master_credentials is required")
    else:
        if not master.get("username"):
            errors.append("master_credentials.username is required")
        if not master.get("password"):
            errors.append("master_credentials.password is required")
    apps = doc.get("applications")
    if not isinstance(apps, list) or not apps:
        errors.append("at least one application is required")
    else:
        for idx, app in enumerate(apps, 1):
            if not isinstance(app, dict):
                errors.append(f"applications[{idx}] must be an object")
                continue
            if not app.get("website_domain"):
                errors.append(f"applications[{idx}].website_domain is required")
            path = app.get("path_to_public_html", "")
            if not path:
                errors.append(f"applications[{idx}].path_to_public_html is required")
            elif provider.casefold() == "cloudways" and (
                not path.startswith("/home/master/applications/") or not path.endswith("/public_html")
            ):
                errors.append(f"applications[{idx}].path_to_public_html must be a Cloudways public_html path")
    return errors


def client_path_for_doc(doc: dict[str, Any], clients_dir: Path = CLIENTS_DIR) -> Path:
    base = slugify(str(doc.get("client_name") or "client"))
    provider = slugify(str(doc.get("hosting_provider") or "Cloudways"))
    candidate = clients_dir / f"{base}_{provider}.json"
    suffix = 2
    while candidate.exists():
        candidate = clients_dir / f"{base}-{suffix}_{provider}.json"
        suffix += 1
    return candidate


def provider_from_client_path(path: Path) -> str:
    suffix = path.stem.rsplit("_", 1)[-1]
    return suffix.replace("-", " ").title() if suffix else "Cloudways"


def write_client_doc(doc: dict[str, Any], clients_dir: Path = CLIENTS_DIR) -> Path:
    errors = validate_client_doc(doc)
    if errors:
        raise ValueError("; ".join(errors))
    clients_dir.mkdir(parents=True, exist_ok=True)
    path = client_path_for_doc(doc, clients_dir)
    path.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def key_path_for_name(name: str, keys_dir: Path | None = None) -> Path:
    keys_dir = keys_dir or KEYS_DIR
    safe_name = slugify(Path(name).name).replace("-", "_")
    if not safe_name:
        safe_name = "ssh_key"
    return keys_dir / safe_name


def write_ssh_key(name: str, content: str, keys_dir: Path | None = None) -> Path:
    keys_dir = keys_dir or KEYS_DIR
    text = content.strip() + "\n"
    if "PRIVATE KEY" not in text.splitlines()[0]:
        raise ValueError("uploaded file does not look like a private SSH key")
    keys_dir.mkdir(parents=True, exist_ok=True)
    keys_dir.chmod(0o700)
    path = key_path_for_name(name, keys_dir)
    suffix = 2
    while path.exists():
        path = keys_dir / f"{path.stem}_{suffix}"
        suffix += 1
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def list_ssh_keys(keys_dir: Path | None = None) -> list[dict[str, str]]:
    keys_dir = keys_dir or KEYS_DIR
    if not keys_dir.exists():
        return []
    return [
        {"name": path.name, "path": str(path)}
        for path in sorted(keys_dir.iterdir())
        if path.is_file()
    ]


def resolve_uploaded_key(name: str, keys_dir: Path | None = None) -> Path | None:
    keys_dir = keys_dir or KEYS_DIR
    if not name:
        return None
    candidate = keys_dir / Path(name).name
    if not candidate.is_file():
        raise ValueError("selected SSH key was not found")
    return candidate


def list_client_files(clients_dir: Path = CLIENTS_DIR, provider: str | None = None) -> list[dict[str, str]]:
    selected_provider = (provider or "").casefold()
    rows: list[dict[str, str]] = []
    for path in sorted(clients_dir.glob("*.json")):
        label = path.stem
        client_provider = provider_from_client_path(path)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            label = str(doc.get("client_name") or label)
            client_provider = str(doc.get("hosting_provider") or client_provider).strip() or client_provider
        except (OSError, json.JSONDecodeError):
            pass
        if selected_provider and client_provider.casefold() != selected_provider:
            continue
        rows.append({"name": path.name, "label": label, "provider": client_provider})
    return rows


def build_wp_args(payload: dict[str, Any], *, remote: bool = False) -> list[str]:
    args = ["wp_update.py", "--stream"]
    if payload.get("execute"):
        args.append("--execute")
    if payload.get("includeWooCommerce"):
        args.append("--include-woocommerce")
    client_file = str(payload.get("clientFile") or "").strip()
    if client_file:
        safe_name = Path(client_file).name
        client_path = f"clients/{safe_name}" if remote else str(CLIENTS_DIR / safe_name)
        args.extend(["--client-file", client_path])
    selected_key = str(payload.get("sshKey") or "").strip()
    if selected_key and not remote:
        key_path = resolve_uploaded_key(selected_key)
        args.extend(["--ssh-key", str(key_path)])
    return args


def build_local_command(payload: dict[str, Any]) -> list[str]:
    args = build_wp_args(payload)
    return [sys.executable, str(SCRIPT_PATH), *args[1:]]


def build_remote_command(payload: dict[str, Any], settings: Settings) -> list[str]:
    host = str(payload.get("remoteHost") or settings.remote_host).strip()
    user = str(payload.get("remoteUser") or settings.remote_user).strip()
    repo_path = str(payload.get("remoteRepoPath") or settings.remote_repo_path).strip()
    port = int(payload.get("remotePort") or settings.remote_port)
    identity_file = str(payload.get("remoteIdentityFile") or settings.remote_identity_file).strip()
    selected_key = str(payload.get("sshKey") or "").strip()
    if selected_key and not identity_file:
        key_path = resolve_uploaded_key(selected_key)
        identity_file = str(key_path)
    remote_python = str(payload.get("remotePython") or settings.remote_python).strip() or "python3"
    if not host or not user or not repo_path:
        raise ValueError("remote host, user, and repo path are required")

    wp_args = build_wp_args(payload, remote=True)
    remote_script = "cd {repo} && exec {python} {args}".format(
        repo=shlex.quote(repo_path),
        python=shlex.quote(remote_python),
        args=" ".join(shlex.quote(part) for part in wp_args),
    )
    command = [
        "ssh",
        "-p",
        str(port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if identity_file:
        command.extend(["-i", identity_file])
    command.extend([f"{user}@{host}", remote_script])
    return command


def start_run(
    payload: dict[str, Any], settings: Settings, *, started_by: str = ""
) -> RunRecord:
    provider = str(payload.get("provider") or "Cloudways").strip() or "Cloudways"
    if provider.casefold() != "cloudways":
        raise ValueError(f"{provider} does not have a runner configured yet")

    target = str(payload.get("target") or "local")
    if target == "remote":
        command = build_remote_command(payload, settings)
    else:
        target = "local"
        command = build_local_command(payload)

    run_id = secrets.token_hex(8)
    mode = "execute" if payload.get("execute") else "dry-run"
    record = RunRecord(
        run_id=run_id,
        command=command,
        mode=mode,
        target=target,
        client_file=str(payload.get("clientFile") or "").strip(),
        include_woo=bool(payload.get("includeWooCommerce")),
        started_by=started_by,
    )
    if DB_CONN is not None:
        try:
            db.insert_run_started(
                DB_CONN,
                webui_run_id=run_id,
                provider=provider,
                mode=mode,
                target=target,
                client_file=record.client_file or None,
                include_woo=record.include_woo,
                started_by=started_by or None,
                started_at=record.started_at,
            )
        except sqlite3.Error as exc:
            # Fail closed: a run we cannot persist would silently disappear
            # from /api/runs after exit because runs_payload only overlays
            # live records that are still in the 'running' state.
            print(f"db insert_run_started failed: {exc}", file=sys.stderr)
            raise RuntimeError(f"could not persist run: {exc}") from exc
    with RUNS_LOCK:
        RUNS[run_id] = record
    thread = threading.Thread(target=_run_process, args=(record,), daemon=True)
    thread.start()
    return record


def cancel_run(run_id: str, *, grace: float = 8.0) -> dict[str, Any]:
    """Request cancellation of a live run.

    Sends SIGTERM to the subprocess group, schedules a SIGKILL after the
    grace window if it is still alive. Idempotent: cancelling a run that
    has already exited (or is not in RUNS) returns the current status.
    """
    with RUNS_LOCK:
        record = RUNS.get(run_id)
    if record is None:
        return {"id": run_id, "status": "unknown"}
    with record.lock:
        record.cancel_requested = True
        record.cancelled_at = time.time()
        proc = record.proc
    if proc is None or proc.poll() is not None:
        return {"id": run_id, "status": record.status}
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"id": run_id, "status": record.status}
    threading.Thread(
        target=_kill_after_grace, args=(record, proc, grace), daemon=True
    ).start()
    return {"id": run_id, "status": "cancelling"}


def _kill_after_grace(record: RunRecord, proc: subprocess.Popen[str], grace: float) -> None:
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.2)
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def _scan_run_metadata(record: RunRecord, line: str) -> None:
    """Pick out wp_update.py's run_id and summary path from streaming output.

    wp_update.py prints two anchor lines:
      - `WordPress Maintenance Run  |  ID: <ts>` (early, before any work)
      - `Summary written to <path>` (right before exit)
    Once we have the run_id we attach it to the DB row so that operators
    can correlate the webui token with the on-disk log/summary names.
    """
    if record.wp_run_id is None:
        match = _RUN_ID_PATTERN.search(line)
        if match:
            record.wp_run_id = match.group(1)
            record.log_path = str(LOGS_DIR / f"wp-update-{record.wp_run_id}.log")
            if DB_CONN is not None:
                try:
                    db.attach_run_id(DB_CONN, record.run_id, record.wp_run_id)
                except sqlite3.Error as exc:
                    print(f"db attach_run_id failed: {exc}", file=sys.stderr)
            return
    if record.summary_path is None:
        match = _SUMMARY_PATTERN.search(line)
        if match:
            record.summary_path = match.group(1).strip()


def _finalize_run_in_db(record: RunRecord) -> None:
    """Mark the run finished, then ingest the summary if it landed.

    Ingest happens in the same worker thread that ran the process: the
    summary file already exists when wp_update.py exits, so there is no
    need to defer this work. If ingest raises, the failure is recorded
    so the startup reconcile can retry on the next boot.
    """
    if DB_CONN is None:
        return
    try:
        db.update_run_finished(
            DB_CONN,
            webui_run_id=record.run_id,
            status=record.status,
            exit_code=record.exit_code,
            log_path=record.log_path,
            summary_path=record.summary_path,
        )
    except sqlite3.Error as exc:
        print(f"db update_run_finished failed: {exc}", file=sys.stderr)
        return
    if not record.summary_path:
        return
    if record.target == "remote":
        # The summary path printed by wp_update.py lives on the remote host,
        # not on the webui filesystem. Skip ingest until a transport step is
        # added; the row stays at ingest_status='none' so it doesn't get
        # retried forever by the startup reconciler.
        db.mark_ingest_failed(
            DB_CONN,
            webui_run_id=record.run_id,
            error="remote summary ingest not yet supported",
        )
        return
    summary_path = Path(record.summary_path)
    if not summary_path.is_absolute():
        summary_path = LOGS_DIR / summary_path.name
    summary = db.load_summary_file(summary_path)
    if summary is None:
        db.mark_ingest_failed(
            DB_CONN, webui_run_id=record.run_id, error="summary file missing or unparseable"
        )
        return
    try:
        db.ingest_run_summary(DB_CONN, webui_run_id=record.run_id, summary=summary)
    except Exception as exc:  # defensive — any ingest failure is recoverable
        db.mark_ingest_failed(DB_CONN, webui_run_id=record.run_id, error=str(exc))


def _run_process(record: RunRecord) -> None:
    record.publish(f"$ {' '.join(shlex.quote(part) for part in record.command)}")
    try:
        proc = subprocess.Popen(
            record.command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        record.publish(f"failed to start: {exc}")
        record.mark_finished(127)
        _finalize_run_in_db(record)
        record.notify_done()
        return
    with record.lock:
        record.proc = proc
    assert proc.stdout is not None
    for line in proc.stdout:
        clean = line.rstrip("\n")
        _scan_run_metadata(record, clean)
        record.publish(clean)
    exit_code = proc.wait()
    # Order matters: persist terminal state and ingest the summary BEFORE
    # SSE listeners get the 'done' event. Otherwise the UI fires its
    # follow-up summary fetch against a row still marked 'running'.
    record.mark_finished(exit_code)
    _finalize_run_in_db(record)
    record.notify_done()


def _live_run_row(record: RunRecord) -> dict[str, Any]:
    """Render an in-memory RunRecord as the same shape recent_runs uses.

    Lets the API merge running rows with finished DB rows without the
    UI caring about the source.
    """
    return {
        "id": record.run_id,
        "run_id": record.wp_run_id,
        "status": record.status,
        "mode": record.mode,
        "target": record.target,
        "client_file": record.client_file or None,
        "startedAt": record.started_at,
        "finished_at": None,
        "exit_code": record.exit_code,
        "ingest_status": "live",
    }


def _db_run_row(row: dict[str, Any]) -> dict[str, Any]:
    started_at = _iso_to_epoch(row.get("started_at"))
    return {
        "id": row["webui_run_id"],
        "run_id": row.get("run_id"),
        "status": row.get("status"),
        "mode": row.get("mode"),
        "target": row.get("target"),
        "client_file": row.get("client_file"),
        "startedAt": started_at,
        "finished_at": row.get("finished_at"),
        "exit_code": row.get("exit_code"),
        "ingest_status": row.get("ingest_status"),
    }


def _iso_to_epoch(value: Any) -> float | None:
    if not value:
        return None
    try:
        return time.mktime(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (TypeError, ValueError):
        return None


def runs_payload(limit: int, client: str | None, status: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if DB_CONN is not None:
        rows.extend(_db_run_row(r) for r in db.recent_runs(
            DB_CONN, limit=limit, client=client, status=status,
        ))
    seen = {row["id"] for row in rows}
    if not status or status == "running":
        with RUNS_LOCK:
            live = [
                _live_run_row(rec)
                for rec in RUNS.values()
                if rec.status == "running" and rec.run_id not in seen
            ]
        if client:
            needle = client.casefold()
            live = [row for row in live if needle in (row.get("client_file") or "").casefold()]
        rows.extend(live)
    rows.sort(key=lambda r: r["startedAt"] or 0, reverse=True)
    return rows[:limit]


def run_summary_payload(run_id: str) -> dict[str, Any] | None:
    if DB_CONN is None:
        return None
    meta = db.get_run(DB_CONN, webui_run_id=run_id)
    if meta is None:
        return None
    rows = db.run_summary_rows(DB_CONN, webui_run_id=run_id)
    return {
        "run_id": meta.get("run_id"),
        "webui_run_id": run_id,
        "status": meta.get("status"),
        "mode": meta.get("mode"),
        "started_at": meta.get("started_at"),
        "finished_at": meta.get("finished_at"),
        "log_path": meta.get("log_path"),
        "summary_path": meta.get("summary_path"),
        "ingest_status": meta.get("ingest_status"),
        "sites": rows["sites"],
        "plugins": rows["plugins"],
    }


def client_history_payload(client_name: str) -> dict[str, Any]:
    if DB_CONN is None:
        return {"client": client_name, "last_touched": None, "last_success": None,
                "recent_failures": [], "recent_runs": []}
    history = db.client_history(DB_CONN, client_name=client_name)
    history["recent_runs"] = [_db_run_row(r) for r in history.get("recent_runs", [])]
    return history


def plugin_stats_payload() -> dict[str, Any]:
    if DB_CONN is None:
        return {"top_failed": [], "since": "30d"}
    return {"top_failed": db.plugin_failure_stats(DB_CONN), "since": "30d"}


def logs_listing_payload() -> list[dict[str, Any]]:
    """Cross-reference on-disk log files with DB run rows.

    The file system is the source of truth for log presence; the DB
    contributes the status/run_id correlation the UI needs to know
    whether a log corresponds to a known run.
    """
    if not LOGS_DIR.exists():
        return []
    db_by_run_id: dict[str, dict[str, Any]] = {}
    if DB_CONN is not None:
        for row in db.recent_runs(DB_CONN, limit=200):
            wp_id = row.get("run_id")
            if wp_id:
                db_by_run_id[wp_id] = row
    logs = []
    for path in sorted(LOGS_DIR.glob("wp-update-*.log"), key=os.path.getmtime, reverse=True):
        wp_id = path.stem.replace("wp-update-", "")
        meta = db_by_run_id.get(wp_id, {})
        logs.append({
            "filename": path.name,
            "run_id": wp_id,
            "webui_run_id": meta.get("webui_run_id"),
            "status": meta.get("status") or "unknown",
            "size_bytes": path.stat().st_size,
            "modified_at": path.stat().st_mtime,
        })
    return logs


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class WebUIHandler(BaseHTTPRequestHandler):
    server_version = "WPUpdateWebUI/1.0"
    settings: Settings

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/login":
            self._send_html(LOGIN_HTML)
            return
        if path == "/logout":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if path.startswith("/api/") and not self._authenticated():
            json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "authentication required"})
            return
        if path == "/":
            if not self._authenticated():
                self._redirect("/login")
                return
            self._send_html(app_html(self.settings))
            return
        if path == "/api/clients":
            query = parse_qs(urlparse(self.path).query)
            provider = query.get("provider", [""])[0]
            json_response(self, HTTPStatus.OK, {"clients": list_client_files(provider=provider)})
            return
        if path == "/api/ssh-keys":
            json_response(self, HTTPStatus.OK, {"keys": list_ssh_keys()})
            return
        if path == "/api/runs":
            query = parse_qs(urlparse(self.path).query)
            limit = max(1, min(200, int(query.get("limit", ["50"])[0] or 50)))
            client_filter = (query.get("client", [""])[0] or "").strip() or None
            status_filter = (query.get("status", [""])[0] or "").strip() or None
            json_response(self, HTTPStatus.OK, {"runs": runs_payload(limit, client_filter, status_filter)})
            return
        if path.startswith("/api/runs/") and path.endswith("/summary"):
            run_id = path.split("/")[3]
            payload = run_summary_payload(run_id)
            if payload is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            json_response(self, HTTPStatus.OK, payload)
            return
        if path.startswith("/api/clients/") and path.endswith("/history"):
            client_name = path.split("/")[3]
            json_response(self, HTTPStatus.OK, client_history_payload(client_name))
            return
        if path == "/api/stats/plugins":
            json_response(self, HTTPStatus.OK, plugin_stats_payload())
            return
        if path == "/api/logs":
            json_response(self, HTTPStatus.OK, {"logs": logs_listing_payload()})
            return
        if path.startswith("/api/logs/"):
            filename = Path(path.split("/")[3]).name
            log_path = LOGS_DIR / filename
            if not log_path.exists() or not filename.startswith("wp-update-") or not filename.endswith(".log"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(log_path.stat().st_size))
            self.end_headers()
            with open(log_path, "rb") as f:
                self.wfile.write(f.read())
            return
        if path.startswith("/api/runs/") and path.endswith("/stream"):
            self._stream_run(path.split("/")[3])
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/login":
            self._login()
            return
        if not self._authenticated():
            json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "authentication required"})
            return
        if not self._csrf_ok():
            json_response(self, HTTPStatus.FORBIDDEN, {"error": "csrf token missing or invalid"})
            return
        if path.startswith("/api/runs/") and path.endswith("/cancel"):
            run_id = path[len("/api/runs/") : -len("/cancel")]
            if not run_id:
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "unknown run"})
                return
            result = cancel_run(run_id)
            if result.get("status") == "unknown":
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "unknown run"})
                return
            json_response(self, HTTPStatus.OK, result)
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        try:
            if path == "/api/clients/import":
                doc = payload.get("client")
                if not isinstance(doc, dict):
                    raise ValueError("client must be an object")
                written = write_client_doc(doc)
                json_response(self, HTTPStatus.CREATED, {"path": written.name})
                return
            if path == "/api/clients/manual":
                written = write_client_doc(manual_payload_to_client(payload))
                json_response(self, HTTPStatus.CREATED, {"path": written.name})
                return
            if path == "/api/ssh-keys":
                name = str(payload.get("name") or "").strip()
                content = str(payload.get("content") or "")
                if not name or not content:
                    raise ValueError("name and content are required")
                written = write_ssh_key(name, content)
                json_response(self, HTTPStatus.CREATED, {"name": written.name, "path": str(written)})
                return
            if path == "/api/runs":
                record = start_run(payload, self.settings, started_by=self._current_user() or "")
                json_response(self, HTTPStatus.CREATED, {"id": record.run_id, "status": record.status})
                return
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except RuntimeError as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _login(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        ip = self.client_address[0] if self.client_address else "unknown"
        if not self.settings.password:
            json_response(self, HTTPStatus.FORBIDDEN, {"error": "WEBUI_PASSWORD is not configured"})
            return
        if DB_CONN is not None:
            allowed, retry_after = db.check_login_rate_limit(
                DB_CONN,
                ip=ip,
                max_failures=LOGIN_MAX_FAILURES,
                within_seconds=LOGIN_FAILURE_WINDOW_SECONDS,
            )
            if not allowed:
                self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
                self.send_header("Retry-After", str(retry_after))
                body = json.dumps({"error": "too many login attempts; try again shortly"}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        if username != self.settings.username or not hmac.compare_digest(password, self.settings.password):
            if DB_CONN is not None:
                db.record_auth_event(DB_CONN, ip=ip, event="login_fail", username=username or None)
            json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "invalid credentials"})
            return
        if DB_CONN is not None:
            db.record_auth_event(DB_CONN, ip=ip, event="login_ok", username=username)
        csrf = secrets.token_urlsafe(32)
        token = sign_session(username, self.settings.secret, csrf=csrf)
        body = json.dumps({"ok": True}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
        )
        # Readable CSRF cookie: JS attaches its value as X-CSRF-Token; server
        # cross-checks against the value baked into the signed session token.
        self.send_header(
            "Set-Cookie", f"{CSRF_COOKIE}={csrf}; Path=/; SameSite=Lax"
        )
        self.end_headers()
        self.wfile.write(body)

    def _expected_csrf(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return None
        return session_csrf(morsel.value, self.settings.secret)

    def _csrf_ok(self) -> bool:
        expected = self._expected_csrf()
        if not expected:
            return False
        supplied = self.headers.get("X-CSRF-Token", "")
        cookie = SimpleCookie(self.headers.get("Cookie")).get(CSRF_COOKIE)
        if not supplied or cookie is None:
            return False
        return (
            hmac.compare_digest(supplied, expected)
            and hmac.compare_digest(cookie.value, expected)
        )

    def _current_user(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return None
        return verify_session(morsel.value, self.settings.secret)

    def _authenticated(self) -> bool:
        return self._current_user() == self.settings.username

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        try:
            payload = json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _send_html(self, body: str) -> None:
        encoded = body.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def _stream_run(self, run_id: str) -> None:
        with RUNS_LOCK:
            record = RUNS.get(run_id)
        if not record:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        listener: queue.Queue[str | None] = queue.Queue()
        with record.lock:
            replay = list(record.lines)
            done = record.status != "running"
            if not done:
                record.listeners.append(listener)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def send_event(event: str, data: str) -> None:
            self.wfile.write(f"event: {event}\n".encode())
            for line in data.splitlines() or [""]:
                self.wfile.write(f"data: {line}\n".encode())
            self.wfile.write(b"\n")
            self.wfile.flush()

        try:
            for line in replay:
                send_event("line", line)
            if done:
                send_event("done", json.dumps({"status": record.status, "exitCode": record.exit_code}))
                return
            while True:
                item = listener.get()
                if item is None:
                    send_event("done", json.dumps({"status": record.status, "exitCode": record.exit_code}))
                    return
                send_event("line", item)
        finally:
            with record.lock:
                if listener in record.listeners:
                    record.listeners.remove(listener)


def manual_payload_to_client(payload: dict[str, Any]) -> dict[str, Any]:
    flags = {
        "wp_cli_installed": bool(payload.get("wpCliInstalled", True)),
        "is_staging": bool(payload.get("isStaging", False)),
        "has_woocommerce": bool(payload.get("hasWooCommerce", False)),
    }
    return {
        "hosting_provider": str(payload.get("provider") or "Cloudways").strip() or "Cloudways",
        "client_name": str(payload.get("clientName") or "").strip(),
        "email": str(payload.get("email") or "").strip(),
        "server_ip_address": str(payload.get("serverIp") or "").strip(),
        "master_credentials": {
            "username": str(payload.get("masterUsername") or "").strip(),
            "password": str(payload.get("masterPassword") or "").strip(),
        },
        "applications": [
            {
                "website_domain": str(payload.get("websiteDomain") or "").strip(),
                "path_to_public_html": str(payload.get("publicHtmlPath") or "").strip(),
                "sftp_credentials": {
                    "username": "$SSH_USER",
                    "password": "$APP_PW",
                    "ssh_key": "$SSH_KEY",
                },
                "environment_flags": flags,
            }
        ],
    }


LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Wordpress Maintenance Login</title>
  <style>
    body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;background:#f5f7f4;color:#18201c;display:grid;min-height:100vh;place-items:center}
    form{width:min(380px,calc(100vw - 32px));background:#fff;border:1px solid #d9ded6;border-radius:8px;padding:28px;box-shadow:0 18px 50px rgba(24,32,28,.08)}
    h1{font-size:24px;margin:0 0 18px}
    label{display:block;font-size:13px;font-weight:700;margin:14px 0 6px}
    input,button{box-sizing:border-box;width:100%;height:42px;border-radius:6px;font:inherit}
    input{border:1px solid #c8cec5;padding:0 12px}
    button{border:0;background:#285b4d;color:#fff;font-weight:800;margin-top:18px;cursor:pointer}
    p{min-height:20px;color:#a33a2a;font-size:13px}
  </style>
</head>
<body>
  <form id="login">
    <h1>Wordpress Maintenance</h1>
    <label>Username</label><input name="username" autocomplete="username" required>
    <label>Password</label><input name="password" type="password" autocomplete="current-password" required>
    <button>Sign in</button>
    <p id="error"></p>
  </form>
  <script>
    document.getElementById('login').addEventListener('submit', async (event) => {
      event.preventDefault();
      const body = Object.fromEntries(new FormData(event.currentTarget));
      const res = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      if (res.ok) location.href = '/';
      else document.getElementById('error').textContent = (await res.json()).error || 'Login failed';
    });
  </script>
</body>
</html>"""


def app_html(settings: Settings) -> str:
    remote_defaults = {
        "host": settings.remote_host,
        "user": settings.remote_user,
        "port": settings.remote_port,
        "repoPath": settings.remote_repo_path,
        "identityFile": settings.remote_identity_file,
        "python": settings.remote_python,
    }
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Wordpress Maintenance Console</title>
  <style>
    :root{{--bg:#f3f5f1;--panel:#fff;--line:#d8ddd3;--text:#18201c;--muted:#657267;--accent:#285b4d;--warn:#9a4b22;--bad:#a33a2a;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
    *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif}}
    header{{min-height:64px;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 24px;border-bottom:1px solid var(--line);background:#fbfcfa;position:sticky;top:0;z-index:3}}
    h1{{font-size:19px;margin:0}} a{{color:var(--accent);font-weight:700;text-decoration:none}}
    .provider-bar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:14px 18px 0;max-width:1500px;margin:0 auto}}
    .provider-tabs{{display:flex;gap:8px;flex-wrap:wrap}} .provider-tabs button{{height:34px;background:#e6ebe3;color:#213129}} .provider-tabs button.active{{background:var(--accent);color:#fff}}
    .provider-add{{display:flex;gap:8px;align-items:center;margin-left:auto}} .provider-add input{{width:190px;height:34px}} .provider-add button{{height:34px}}
    main{{display:grid;grid-template-columns:minmax(300px,420px) 1fr;gap:18px;padding:18px;max-width:1500px;margin:0 auto}}
    section{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px}}
    h2{{font-size:15px;margin:0 0 14px}} h3{{font-size:13px;margin:18px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}}
    label{{display:block;font-size:12px;font-weight:800;margin:10px 0 5px;color:#334139}}
    input,select,textarea,button{{font:inherit;border-radius:6px}} input,select,textarea{{width:100%;border:1px solid #c8cec5;padding:9px 10px;background:#fff;color:var(--text)}} textarea{{min-height:154px;font-family:var(--mono);font-size:12px}}
    .row{{display:grid;grid-template-columns:1fr 1fr;gap:10px}} .toggles{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}}
    .check{{display:flex;gap:8px;align-items:center;border:1px solid var(--line);border-radius:6px;padding:10px;background:#fafbf9}} .check input{{width:auto}}
    button{{height:40px;border:0;padding:0 14px;background:var(--accent);color:white;font-weight:800;cursor:pointer}} button.secondary{{background:#e6ebe3;color:#213129}} button.danger{{background:var(--warn)}} button:disabled{{opacity:.55;cursor:not-allowed}}
    .actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}} .status{{font-size:12px;color:var(--muted);min-height:18px;margin-top:8px}}
    #terminal{{height:620px;overflow:auto;background:#101511;color:#d9f7df;border-radius:8px;padding:14px;font-family:var(--mono);font-size:12px;line-height:1.5;white-space:pre-wrap;border:1px solid #26362a}}
    .meta{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}} .pill{{border:1px solid var(--line);background:#f7f8f5;color:var(--muted);border-radius:999px;padding:5px 9px;font-size:12px}}
    .tabs{{display:flex;gap:8px;margin-bottom:12px}} .tabs button{{background:#e6ebe3;color:#213129}} .tabs button.active{{background:var(--accent);color:#fff}}
    .hidden{{display:none}} .warn{{color:var(--bad);font-weight:800}}
    table{{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}} th,td{{text-align:left;padding:8px;border-bottom:1px solid var(--line)}} th{{color:var(--muted);font-weight:800;text-transform:uppercase;font-size:10px}}
    .modal{{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:100}}
    .modal-content{{background:var(--panel);padding:24px;border-radius:8px;width:min(900px,95vw);max-height:90vh;overflow:auto;position:relative}}
    .close-modal{{position:absolute;top:12px;right:16px;font-size:24px;cursor:pointer;background:none;border:0;color:var(--muted)}}
    .collapsible{{cursor:pointer;display:flex;align-items:center;gap:8px;user-select:none}} .collapsible::before{{content:'▶';font-size:10px;transition:transform .2s}} .collapsible.open::before{{transform:rotate(90deg)}}
    .client-history{{background:#f8faf7;border:1px solid var(--line);border-radius:6px;padding:12px;margin-top:10px;font-size:12px}}
    .client-history h4{{margin:0 0 8px;font-size:11px;text-transform:uppercase;color:var(--muted)}}
    .stat-card{{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px;margin-top:10px}}
    .stat-item{{background:#f8faf7;border:1px solid var(--line);padding:10px;border-radius:6px}}
    .stat-value{{font-size:18px;font-weight:800;color:var(--accent)}} .stat-label{{font-size:10px;color:var(--muted);text-transform:uppercase}}
    .cancel-btn{{background:var(--bad);padding:2px 6px;height:auto;font-size:10px;margin-left:8px;border-radius:4px}}
    @media (max-width: 920px){{main{{grid-template-columns:1fr;padding:12px}} #terminal{{height:420px}} header{{padding:12px 14px}} .provider-add{{width:100%;margin-left:0}} .provider-add input{{width:100%}}}}


  </style>
</head>
<body>
  <div id="tlsBanner" class="hidden" style="background:var(--warn);color:#fff;padding:8px;text-align:center;font-size:13px;font-weight:700">
    Warning: serving over HTTP. Credentials are sent unencrypted. Use a reverse proxy with TLS in production.
  </div>
  <header><h1>Wordpress Maintenance Console</h1><a href="/logout">Logout</a></header>
  <nav class="provider-bar" aria-label="Hosting providers">
    <div class="provider-tabs" id="providerTabs"></div>
    <div class="provider-add">
      <input id="newProvider" placeholder="Add provider">
      <button class="secondary" id="addProvider">Add</button>
    </div>
  </nav>
  <main>
    <div>
      <section>
        <h2>Run maintenance</h2>
        <p class="status" id="providerStatus"></p>
        <label>Target</label>
        <select id="target"><option value="local">Local repo</option><option value="remote">Remote server via SSH</option></select>
        <div class="toggles">
          <label class="check"><input id="execute" type="checkbox"> Execute mode</label>
          <label class="check"><input id="includeWoo" type="checkbox"> Include WooCommerce</label>
        </div>
        <label>Client scope</label>
        <select id="clientFile"><option value="">All client files</option></select>
        <div id="clientHistory" class="client-history hidden"></div>
        <label>SSH key</label>
        <select id="sshKey"><option value="">Use .env / remote default</option></select>
        <div class="actions">
          <input id="sshKeyFile" type="file">
          <button class="secondary" id="uploadSshKey">Upload key</button>
        </div>
        <div id="remoteFields" class="hidden">
          <h3>Remote execution</h3>
          <div class="row"><div><label>Host</label><input id="remoteHost"></div><div><label>Port</label><input id="remotePort" type="number" min="1"></div></div>
          <label>User</label><input id="remoteUser">
          <label>Repo path</label><input id="remoteRepoPath">
          <label>Identity file</label><input id="remoteIdentityFile">
          <label>Python</label><input id="remotePython">
        </div>
        <p class="status warn" id="executeWarn"></p>
        <div class="actions"><button id="startRun">Start run</button><button class="secondary" id="refreshClients">Refresh clients</button></div>
        <p class="status" id="runStatus"></p>
      </section>
      <section style="margin-top:18px">
        <h2 class="collapsible" id="toggleRecentRuns">Recent runs</h2>
        <div id="recentRunsPanel">
          <div class="row" style="margin-bottom:10px">
            <input id="filterClient" placeholder="Filter by client..." style="font-size:11px;padding:6px">
            <select id="filterStatus" style="font-size:11px;padding:6px">
              <option value="">All statuses</option>
              <option value="running">Running</option>
              <option value="success">Success</option>
              <option value="failed">Failed</option>
            </select>
          </div>
          <table id="recentRunsTable">
            <thead><tr><th>ID</th><th>Status</th><th>Target</th><th>Started</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </section>
      <section style="margin-top:18px">
        <h2>Add client</h2>
        <div class="tabs"><button class="active" data-tab="manual">Manual</button><button data-tab="import">JSON import</button></div>
        <div id="manualTab">
          <label>Client name</label><input id="clientName">
          <div class="row"><div><label>Email</label><input id="email"></div><div><label>Server IP</label><input id="serverIp"></div></div>
          <div class="row"><div><label>Master username</label><input id="masterUsername"></div><div><label>Master password</label><input id="masterPassword" type="password"></div></div>
          <label>Website domain</label><input id="websiteDomain">
          <label>Public HTML path</label><input id="publicHtmlPath" placeholder="/home/master/applications/appid/public_html">
          <div class="toggles">
            <label class="check"><input id="isStaging" type="checkbox"> Staging</label>
            <label class="check"><input id="hasWoo" type="checkbox"> WooCommerce</label>
          </div>
          <button id="saveManual">Save client</button>
        </div>
        <div id="importTab" class="hidden">
          <label>JSON file</label><input id="jsonFile" type="file" accept="application/json,.json">
          <label>Client JSON</label><textarea id="jsonImport" spellcheck="false"></textarea>
          <button id="saveImport">Import JSON</button>
        </div>
        <p class="status" id="clientStatus"></p>
      </section>
    </div>
    <section>
      <div class="meta"><span class="pill" id="providerPill">Cloudways</span><span class="pill" id="modePill">dry-run</span><span class="pill" id="targetPill">local</span><span class="pill" id="statusPill">idle</span></div>
      <div id="terminal">No run started.</div>
      
      <div id="runSummary" class="hidden" style="margin-top:18px">
        <h2 class="collapsible open" id="toggleSummary">Run Summary</h2>
        <div id="summaryPanel"></div>
      </div>

      <div style="margin-top:18px">
        <h2 class="collapsible" id="toggleStats">Plugin health (last 30d)</h2>
        <div id="statsPanel" class="hidden">
          <table id="statsTable">
            <thead><tr><th>Plugin</th><th>Fails</th><th>Skips</th><th>Last Fail</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <div style="margin-top:18px">
        <h2 class="collapsible" id="toggleLogs">Logs</h2>
        <div id="logsPanel" class="hidden">
          <table id="logsTable">
            <thead><tr><th>File</th><th>Run ID</th><th>Size</th><th>Modified</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </section>
  </main>
  <script>
    const remoteDefaults = {json.dumps(remote_defaults)};
    const $ = (id) => document.getElementById(id);
    Object.assign($('remoteHost'), {{value: remoteDefaults.host}});
    Object.assign($('remoteUser'), {{value: remoteDefaults.user}});
    Object.assign($('remotePort'), {{value: remoteDefaults.port}});
    Object.assign($('remoteRepoPath'), {{value: remoteDefaults.repoPath}});
    Object.assign($('remoteIdentityFile'), {{value: remoteDefaults.identityFile}});
    Object.assign($('remotePython'), {{value: remoteDefaults.python}});

    if (location.protocol === 'http:' && !['localhost', '127.0.0.1'].includes(location.hostname)) {{
      $('tlsBanner').classList.remove('hidden');
    }}

    function getCookie(name) {{
      const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
      return match ? match[2] : null;
    }}

    async function api(path, options={{}}) {{
      const headers = {{'Content-Type':'application/json', ...options.headers}};
      const csrf = getCookie('csrf_token');
      if (csrf && ['POST', 'DELETE'].includes(options.method?.toUpperCase())) {{
        headers['X-CSRF-Token'] = csrf;
      }}
      const res = await fetch(path, {{...options, headers}});
      const body = await res.json().catch(() => ({{}}));
      if (!res.ok) throw new Error(body.error || res.statusText);
      return body;
    }}
    async function loadClients() {{
      const data = await api(`/api/clients?provider=${{encodeURIComponent(selectedProvider)}}`);
      const select = $('clientFile');
      select.innerHTML = '<option value="">All client files</option>';
      data.clients.forEach((client) => {{
        const option = document.createElement('option');
        option.value = client.name;
        option.textContent = `${{client.label}} (${{client.name}})`;
        select.appendChild(option);
      }});
    }}
    async function loadSshKeys() {{
      const data = await api('/api/ssh-keys');
      const select = $('sshKey');
      const previous = select.value;
      select.innerHTML = '<option value="">Use .env / remote default</option>';
      data.keys.forEach((key) => {{
        const option = document.createElement('option');
        option.value = key.name;
        option.textContent = key.name;
        select.appendChild(option);
      }});
      if ([...select.options].some((option) => option.value === previous)) select.value = previous;
    }}
    function runPayload() {{
      return {{
        provider: selectedProvider,
        target: $('target').value,
        execute: $('execute').checked,
        includeWooCommerce: $('includeWoo').checked,
        clientFile: $('clientFile').value,
        sshKey: $('sshKey').value,
        remoteHost: $('remoteHost').value,
        remotePort: Number($('remotePort').value || 22),
        remoteUser: $('remoteUser').value,
        remoteRepoPath: $('remoteRepoPath').value,
        remoteIdentityFile: $('remoteIdentityFile').value,
        remotePython: $('remotePython').value
      }};
    }}
    const defaultProviders = ['Cloudways', 'Siteground', 'Cloudron'];
    const storedProviders = JSON.parse(localStorage.getItem('maintenanceProviders') || '[]');
    let providers = [...new Set([...defaultProviders, ...storedProviders])];
    let selectedProvider = localStorage.getItem('selectedProvider') || 'Cloudways';
    function saveProviders() {{
      localStorage.setItem('maintenanceProviders', JSON.stringify(providers.filter((name) => !defaultProviders.includes(name))));
      localStorage.setItem('selectedProvider', selectedProvider);
    }}
    function renderProviders() {{
      if (!providers.includes(selectedProvider)) selectedProvider = providers[0] || 'Cloudways';
      const tabs = $('providerTabs');
      tabs.innerHTML = '';
      providers.forEach((provider) => {{
        const isImplemented = provider.toLowerCase() === 'cloudways';
        const button = document.createElement('button');
        button.innerHTML = provider + (!isImplemented ? ' <span style="font-size:9px;opacity:0.7">(soon)</span>' : '');
        button.classList.toggle('active', provider === selectedProvider);
        if (!isImplemented) button.style.opacity = '0.7';
        button.addEventListener('click', () => {{
          selectedProvider = provider;
          saveProviders();
          renderProviders();
          loadClients().catch((error) => $('runStatus').textContent = error.message);
        }});
        tabs.appendChild(button);
      }});
      $('providerPill').textContent = selectedProvider;
      const runnable = selectedProvider.toLowerCase() === 'cloudways';
      $('startRun').style.opacity = runnable ? '1' : '0.6';
      $('providerStatus').textContent = runnable ? 'Cloudways runner: wp_update.py' : `${{selectedProvider}} tab is ready for inventory; no runner is configured yet.`;
      $('publicHtmlPath').placeholder = runnable ? '/home/master/applications/appid/public_html' : 'Provider-specific WordPress path';
    }}
    function appendLine(line) {{
      const terminal = $('terminal');
      if (terminal.textContent === 'No run started.') terminal.textContent = '';
      terminal.textContent += line + '\\n';
      terminal.scrollTop = terminal.scrollHeight;
    }}

    // Phase B: Collapsible panels
    document.querySelectorAll('.collapsible').forEach(h2 => {{
      h2.addEventListener('click', () => {{
        h2.classList.toggle('open');
        const panel = h2.nextElementSibling;
        if (panel) panel.classList.toggle('hidden');
      }});
    }});

    // Phase B: Modal for log viewing
    function showModal(content, title = '') {{
      const modal = document.createElement('div');
      modal.className = 'modal';
      modal.innerHTML = `
        <div class="modal-content">
          <button class="close-modal">&times;</button>
          <h2>${{title}}</h2>
          <pre style="white-space:pre-wrap;font-family:var(--mono);font-size:12px;background:#f8faf7;padding:12px;border:1px solid var(--line);border-radius:6px">${{content}}</pre>
        </div>
      `;
      document.body.appendChild(modal);
      modal.querySelector('.close-modal').onclick = () => modal.remove();
      modal.onclick = (e) => {{ if (e.target === modal) modal.remove(); }};
    }}

    // B1: Recent runs
    async function loadRecentRuns() {{
      const client = $('filterClient').value;
      const status = $('filterStatus').value;
      try {{
        const data = await api(`/api/runs?limit=50&client=${{encodeURIComponent(client)}}&status=${{encodeURIComponent(status)}}`);
        const tbody = $('recentRunsTable').querySelector('tbody');
        tbody.innerHTML = '';
        data.runs.forEach(run => {{
          const tr = document.createElement('tr');
          tr.style.cursor = 'pointer';
          const cancelBtn = run.status === 'running' ? `<button class="cancel-btn" data-id="${{run.id}}">Cancel</button>` : '';
          tr.innerHTML = `
            <td>${{run.id}}</td>
            <td><span class="pill">${{run.status}}</span>${{cancelBtn}}</td>
            <td>${{run.target}}</td>
            <td>${{new Date(run.startedAt * 1000).toLocaleString()}}</td>
          `;
          tr.onclick = (e) => {{
            if (e.target.tagName === 'BUTTON') return;
            if (run.status === 'running') {{
              reAttachSSE(run.id);
            }} else {{
              showRunSummary(run.id);
            }}
          }};
          tbody.appendChild(tr);
        }});
        tbody.querySelectorAll('.cancel-btn').forEach(btn => {{
          btn.onclick = async () => {{
            try {{
              btn.disabled = true;
              await api(`/api/runs/${{btn.dataset.id}}/cancel`, {{method: 'POST'}});
              loadRecentRuns();
            }} catch (e) {{
              alert('Cancel failed: ' + e.message);
              btn.disabled = false;
            }}
          }};
        }});
      }} catch (e) {{ console.error('Failed to load runs', e); }}
    }}
    $('filterClient').addEventListener('input', loadRecentRuns);
    $('filterStatus').addEventListener('change', loadRecentRuns);

    function reAttachSSE(runId) {{
      $('terminal').textContent = '';
      $('statusPill').textContent = 'running';
      $('runStatus').textContent = `Re-attached to run ${{runId}}`;
      const stream = new EventSource(`/api/runs/${{runId}}/stream`);
      stream.addEventListener('line', (event) => appendLine(event.data));
      stream.addEventListener('done', (event) => {{
        const done = JSON.parse(event.data);
        $('statusPill').textContent = `${{done.status}} (${{done.exitCode}})`;
        appendLine(`run finished: ${{done.status}} exit=${{done.exitCode}}`);
        loadRecentRuns();
        showRunSummary(runId);
        stream.close();
      }});
    }}

    // B2: Client history
    $('clientFile').addEventListener('change', async () => {{
      const name = $('clientFile').value;
      const container = $('clientHistory');
      if (!name) {{ container.classList.add('hidden'); return; }}
      try {{
        const data = await api(`/api/clients/${{encodeURIComponent(name)}}/history`);
        container.innerHTML = `
          <h4>${{data.client}} History</h4>
          <div>Last touched: ${{data.last_touched}}</div>
          <div>Last success: ${{data.last_success}}</div>
          ${{data.recent_failures.length ? `<div class="warn">Recent failures: ${{data.recent_failures.length}}</div>` : ''}}
        `;
        container.classList.remove('hidden');
      }} catch (e) {{ console.error('Failed to load client history', e); }}
    }});

    // B3: Plugin health
    async function loadPluginStats() {{
      try {{
        const data = await api('/api/stats/plugins');
        const tbody = $('statsTable').querySelector('tbody');
        tbody.innerHTML = '';
        data.top_failed.forEach(s => {{
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${{s.plugin}}</td>
            <td class="warn">${{s.fail_count}}</td>
            <td>${{s.skip_count}}</td>
            <td>${{new Date(s.last_failure_at * 1000).toLocaleDateString()}}</td>
          `;
          tbody.appendChild(tr);
        }});
      }} catch (e) {{ console.error('Failed to load stats', e); }}
    }}

    // B4: Run Summary
    async function showRunSummary(runId) {{
      try {{
        const data = await api(`/api/runs/${{runId}}/summary`);
        const panel = $('summaryPanel');
        panel.innerHTML = `
          <div class="stat-card">
            <div class="stat-item"><div class="stat-value">${{data.sites.length}}</div><div class="stat-label">Sites</div></div>
            <div class="stat-item"><div class="stat-value">${{data.plugins.length}}</div><div class="stat-label">Updates</div></div>
            <div class="stat-item"><div class="stat-value">${{data.sites.filter(s=>s.outcome==='success').length}}</div><div class="stat-label">Success</div></div>
          </div>
          <table style="margin-top:14px">
            <thead><tr><th>Domain</th><th>Outcome</th><th>Reason</th></tr></thead>
            <tbody>
              ${{data.sites.map(s => `<tr><td>${{s.domain}}</td><td>${{s.outcome}}</td><td>${{s.reason}}</td></tr>`).join('')}}
            </tbody>
          </table>
        `;
        $('runSummary').classList.remove('hidden');
      }} catch (e) {{ console.error('Failed to load summary', e); }}
    }}

    // B5: Logs
    async function loadLogs() {{
      try {{
        const data = await api('/api/logs');
        const tbody = $('logsTable').querySelector('tbody');
        tbody.innerHTML = '';
        data.logs.forEach(log => {{
          const tr = document.createElement('tr');
          tr.style.cursor = 'pointer';
          tr.innerHTML = `
            <td>${{log.filename}}</td>
            <td>${{log.run_id}}</td>
            <td>${{(log.size_bytes/1024).toFixed(1)}} KB</td>
            <td>${{new Date(log.modified_at * 1000).toLocaleString()}}</td>
          `;
          tr.onclick = async () => {{
            const text = await fetch(`/api/logs/${{log.filename}}`).then(r => r.text());
            showModal(text, log.filename);
          }};
          tbody.appendChild(tr);
        }});
      }} catch (e) {{ console.error('Failed to load logs', e); }}
    }}

    $('target').addEventListener('change', () => {{
      $('remoteFields').classList.toggle('hidden', $('target').value !== 'remote');
      $('targetPill').textContent = $('target').value;
    }});
    $('execute').addEventListener('change', () => {{
      $('modePill').textContent = $('execute').checked ? 'execute' : 'dry-run';
      $('executeWarn').textContent = $('execute').checked ? 'Execute mode will perform remote writes and backups.' : '';
    }});
    $('startRun').addEventListener('click', async () => {{
      if (selectedProvider.toLowerCase() !== 'cloudways') {{
        $('runStatus').textContent = `Runner not yet implemented for ${{selectedProvider}}`;
        return;
      }}
      try {{
        const data = await api('/api/runs', {{method:'POST', body:JSON.stringify(runPayload())}});
        $('terminal').textContent = '';
        $('statusPill').textContent = data.status;
        $('runStatus').textContent = `Run ${{data.id}} started`;
        loadRecentRuns();
        const stream = new EventSource(`/api/runs/${{data.id}}/stream`);
        stream.addEventListener('line', (event) => appendLine(event.data));
        stream.addEventListener('done', (event) => {{
          const done = JSON.parse(event.data);
          $('statusPill').textContent = `${{done.status}} (${{done.exitCode}})`;
          appendLine(`run finished: ${{done.status}} exit=${{done.exitCode}}`);
          loadRecentRuns();
          showRunSummary(data.id);
          stream.close();
        }});
      }} catch (error) {{ $('runStatus').textContent = error.message; }}
    }});
    $('refreshClients').addEventListener('click', loadClients);
    $('uploadSshKey').addEventListener('click', async () => {{
      const file = $('sshKeyFile').files[0];
      if (!file) {{ $('runStatus').textContent = 'Choose an SSH key file first.'; return; }}
      try {{
        const data = await api('/api/ssh-keys', {{method:'POST', body:JSON.stringify({{name:file.name, content:await file.text()}})}});
        $('runStatus').textContent = `Uploaded SSH key ${{data.name}}`;
        await loadSshKeys();
        $('sshKey').value = data.name;
      }} catch (error) {{ $('runStatus').textContent = error.message; }}
    }});
    $('addProvider').addEventListener('click', () => {{
      const name = $('newProvider').value.trim();
      if (!name) return;
      providers = [...new Set([...providers, name])];
      selectedProvider = name;
      $('newProvider').value = '';
      saveProviders();
      renderProviders();
      loadClients().catch((error) => $('runStatus').textContent = error.message);
    }});
    document.querySelectorAll('.tabs button').forEach((button) => {{
      button.addEventListener('click', () => {{
        document.querySelectorAll('.tabs button').forEach((b) => b.classList.remove('active'));
        button.classList.add('active');
        $('manualTab').classList.toggle('hidden', button.dataset.tab !== 'manual');
        $('importTab').classList.toggle('hidden', button.dataset.tab !== 'import');
      }});
    }});
    $('saveManual').addEventListener('click', async () => {{
      const ids = ['clientName','email','serverIp','masterUsername','masterPassword','websiteDomain','publicHtmlPath'];
      const payload = Object.fromEntries(ids.map((id) => [id, $(id).value]));
      payload.provider = selectedProvider;
      payload.isStaging = $('isStaging').checked; payload.hasWooCommerce = $('hasWoo').checked;
      try {{ const data = await api('/api/clients/manual', {{method:'POST', body:JSON.stringify(payload)}}); $('clientStatus').textContent = `Saved ${{data.path}}`; await loadClients(); }}
      catch (error) {{ $('clientStatus').textContent = error.message; }}
    }});
    $('saveImport').addEventListener('click', async () => {{
      try {{
        const client = JSON.parse($('jsonImport').value);
        const data = await api('/api/clients/import', {{method:'POST', body:JSON.stringify({{client}})}});
        $('clientStatus').textContent = `Imported ${{data.path}}`; await loadClients();
      }} catch (error) {{ $('clientStatus').textContent = error.message; }}
    }});
    $('jsonFile').addEventListener('change', async (event) => {{
      const file = event.target.files[0];
      if (file) $('jsonImport').value = await file.text();
    }});
    renderProviders();
    loadSshKeys().catch((error) => $('runStatus').textContent = error.message);
    loadClients().catch((error) => $('runStatus').textContent = error.message);
    loadRecentRuns();
    loadPluginStats();
    loadLogs();
  </script>
</body>
</html>"""


def make_handler(settings: Settings) -> type[WebUIHandler]:
    class ConfiguredHandler(WebUIHandler):
        pass

    ConfiguredHandler.settings = settings
    return ConfiguredHandler


def build_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the authenticated WordPress maintenance web UI.")
    parser.add_argument("--host", default=None, help="Bind host (default: WEBUI_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: WEBUI_PORT or 8787)")
    return parser.parse_args()


def main() -> int:
    args = build_cli()
    settings = Settings.from_env()
    if args.host:
        settings = Settings(**{**settings.__dict__, "host": args.host})
    if args.port:
        settings = Settings(**{**settings.__dict__, "port": args.port})
    if not settings.password:
        print("Refusing to start: set WEBUI_PASSWORD first.", file=sys.stderr)
        return 2
    init_db()
    server = ThreadingHTTPServer((settings.host, settings.port), make_handler(settings))
    print(f"Serving Cloudways maintenance UI at http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web UI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
