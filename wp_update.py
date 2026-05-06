#!/usr/bin/env python3
"""
wp_update.py — Production-grade WordPress maintenance automation for Cloudways.

Safely updates WordPress core, themes, and plugins across multiple client sites
hosted on Cloudways. Prioritises zero-downtime and rapid rollback.

Design principles:
  1. Dry-run by default — pass --execute to perform remote writes.
  2. Atomic sequential updates — plugins are updated ONE AT A TIME with an HTTP
     health-check after each, so the exact point of failure is always known.
  3. Pre-flight backups — full DB export + filesystem tar BEFORE any mutation.
  4. Automatic rollback — on any failure (non-zero exit OR 5xx HTTP), the site
     is restored from its pre-flight backup immediately.
  5. Credential safety — passwords and key paths are NEVER written to log files
     or summary JSON.
  6. Graceful degradation — incomplete client JSON files are logged and skipped,
     they do not crash the entire run.
  7. WooCommerce caution — sites with has_woocommerce=true are flagged for
     manual review and skipped unless --include-woocommerce is passed.

Usage:
  # Dry-run (default) — collects baselines, plans backups, touches nothing
  python3 wp_update.py

  # Live execution against all clients
  python3 wp_update.py --execute

  # Single client
  python3 wp_update.py --execute --client-file ../clients/amy_cloudways.json

  # Include WooCommerce sites (normally skipped for safety)
  python3 wp_update.py --execute --include-woocommerce

SSH execution strategy:
  Scripts are piped to the remote host via stdin rather than passed as SSH
  positional arguments. This avoids a class of quoting bugs where multi-line
  scripts are mangled by SSH's argument concatenation. The remote command is:
    ssh [opts] user@host bash -ls
  where -l = login shell (loads PATH for wp-cli) and -s = read from stdin.
  subprocess.run(input=script) delivers the script body over stdin.

Rollback mechanism:
  Before ANY update, the script creates:
    1. A full database dump via `wp db export --add-drop-table`
    2. A compressed tar of the entire public_html directory
  Both are stored under <app_dir>/private_html/wp-maintenance-backups/<run_id>/
  which is persistent storage writable by the app SSH user (not /tmp/).

  If an update step fails:
    1. The failed state is archived (for forensic analysis)
    2. The public_html directory is wiped and restored from the tar
    3. The database is restored via `wp db import`
    4. An HTTP health-check confirms the rollback succeeded

  If the ROLLBACK itself fails, the script logs the failure and moves on —
  the pre-flight backup files remain on disk for manual recovery.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent            # claude-wordpress-maintenance/
DEFAULT_ENV = SCRIPT_DIR / ".env"
DEFAULT_CLIENTS = SCRIPT_DIR / "clients"
DEFAULT_LOGS = SCRIPT_DIR / "logs"
DEFAULT_DB = SCRIPT_DIR / "db" / "wpmaint.db"
DEFAULT_SSH_CONFIG = Path(os.environ.get("WP_UPDATE_SSH_CONFIG", "/dev/null"))

# Cloudways apps always live under /home/master/applications/<hash>/public_html
VALID_PATH = re.compile(r"^/home/master/applications/[A-Za-z0-9_-]+/public_html$")

# Strings that indicate a fatal PHP crash when found in page body
FATAL_MARKERS = (
    "fatal error",
    "there has been a critical error",
    "uncaught exception",
    "parse error",
    "stack trace",
)

# Known backup/migration plugins — if present, the site has an alternative
# backup mechanism beyond our script's own pre-flight backup.
# Note: jetpack-backup is the actual backup add-on; the base "jetpack" slug
# does NOT imply backup capability.
KNOWN_BACKUP_PLUGINS = {
    "updraftplus":              "UpdraftPlus",
    "backwpup":                 "BackWPup",
    "duplicator":               "Duplicator",
    "duplicator-pro":           "Duplicator Pro",
    "all-in-one-wp-migration":  "All-in-One WP Migration",
    "blogvault-real-time-backup": "BlogVault",
    "wpvivid-backuprestore":    "WPvivid",
    "backup-backup":            "Backup Migration",
    "jetpack-backup":           "Jetpack Backup",
    "backupwordpress":          "BackUpWordPress",
}

# Confidence-scoring rules used by _compute_confidence. Tunable in one place.
CONFIDENCE_RULES = {
    "woocommerce_penalty": 15,
    "plugin_updates_high_threshold": 10,
    "plugin_updates_high_penalty": 20,
    "plugin_updates_med_threshold": 5,
    "plugin_updates_med_penalty": 10,
    "plugin_updates_low_penalty": 3,
    "theme_updates_penalty": 5,
    "core_update_penalty": 5,
    "large_site_threshold_mb": 2000,
    "large_site_penalty": 5,
    "tight_disk_multiplier": 3,
    "tight_disk_penalty": 10,
    "old_php_threshold": 8.0,
    "old_php_penalty": 10,
    "no_backup_plugin_penalty": 5,
    "staging_bonus": 10,
    "grade_high_min": 90,
    "grade_medium_min": 70,
    "grade_low_min": 50,
}

# Tolerant classification of WP-CLI's `wp plugin update` status field.
# WP-CLI status strings have drifted across versions (e.g. "Updated" vs
# "updated successfully" vs "success"), so we normalise via .strip().lower()
# before membership-testing.  Anything outside these two sets is treated as
# an error.
_PLUGIN_STATUS_SUCCESS = frozenset({"updated", "success", "updated successfully"})
_PLUGIN_STATUS_UPTODATE = frozenset({"up to date", "already up to date"})


def _extract_plugin_error(result: dict) -> str:
    """Pull a short error message out of a wp-cli plugin-update result dict.

    Prefers explicit 'message'/'error' keys, falls back to the captured
    SSH/parse-error text, truncating to ~200 chars so we never dump a
    full stderr into the JSON report.
    """
    for key in ("message", "error", "_error", "_raw"):
        val = result.get(key)
        if val:
            text = str(val).strip().splitlines()[0] if "\n" in str(val) else str(val).strip()
            return text[:200]
    if result.get("_no_entry"):
        return "wp-cli returned no entry for this slug"
    if result.get("_parse_error"):
        # Reached when _raw is empty/whitespace: wp-cli exited 0 but emitted
        # nothing on stdout or stderr. Surfaces as its own diagnostic so we
        # don't conflate it with a real wp-cli "Error" status.
        return "wp-cli produced no output (likely transient)"
    return f"status={result.get('status', '?')}"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """One atomic operation within a site update."""
    name: str
    status: str                    # success | failed | skipped | planned
    started: str
    ended: str
    detail: str = ""


@dataclass
class SiteReport:
    """Aggregate result for one WordPress application."""
    client: str
    domain: str
    server_ip: str
    wp_path: str
    is_staging: bool
    has_woocommerce: bool
    overall: str = "pending"       # pending | dry-run | success | failed | rolled-back | skipped
    backup_dir: str = ""
    failure_step: str = ""
    failure_detail: str = ""
    rollback_result: str = ""
    baseline: dict[str, Any] = field(default_factory=dict)
    steps: list[StepResult] = field(default_factory=list)
    # Per-site configured skips loaded from sibling notes.json. Each entry:
    # {"type": "plugin"|"theme", "slug": "...", "reason": "..."}
    skip_items: list[dict[str, Any]] = field(default_factory=list)

    # Auth method: "key" (wpupdates SSH key) or "master" (master_xxx + password)
    # Determined during ssh-preflight. When "master", ownership must be restored
    # after any file-mutating operation.
    auth_method: str = "key"

    # The username that actually authenticated at preflight time — either the
    # winning tier-1 candidate (e.g. "wpupdates-stage") or the master user.
    # Always populated post-preflight so the summary JSON records which user
    # worked, letting us later bake it into the client JSON.
    auth_user: str = ""

    # Original user:group of the wp_path directory, captured before mutations.
    # Used to chown -R back after updates when running as master user.
    original_owner: str = ""

    # These are used at runtime but NEVER serialised (see to_dict)
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_key_path: str = ""
    master_user: str = ""
    master_password: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to dict, stripping credentials."""
        return {
            "client": self.client,
            "domain": self.domain,
            "server_ip": self.server_ip,
            "wp_path": self.wp_path,
            "is_staging": self.is_staging,
            "has_woocommerce": self.has_woocommerce,
            "overall": self.overall,
            "auth_method": self.auth_method,
            "auth_user": self.auth_user,
            "original_owner": self.original_owner,
            "backup_dir": self.backup_dir,
            "failure_step": self.failure_step,
            "failure_detail": self.failure_detail,
            "rollback_result": self.rollback_result,
            "baseline": self.baseline,
            "skip_items": self.skip_items,
            "steps": [
                {"name": s.name, "status": s.status,
                 "started": s.started, "ended": s.ended, "detail": s.detail}
                for s in self.steps
            ],
        }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InventoryError(RuntimeError):
    """A client JSON file is invalid or incomplete."""


class SSHError(RuntimeError):
    """A remote command returned non-zero."""


class HealthCheckError(RuntimeError):
    """Post-update HTTP or WP-CLI verification failed."""


class WPCliError(RuntimeError):
    """A wp-cli command produced unparseable output (e.g. malformed JSON)."""


class RollbackFailed(RuntimeError):
    """The rollback itself failed — manual intervention needed."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts() -> str:
    """Current UTC timestamp in ISO-8601."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_env(path: Path) -> dict[str, str]:
    """
    Parse a shell-style .env file.  Handles:
      export KEY="value"
      KEY='value'
      KEY=value
      # comments
    Expands ~ and $HOME in values.
    """
    if not path.exists():
        raise FileNotFoundError(f".env not found: {path}")

    env: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        # Strip matching quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        val = os.path.expanduser(os.path.expandvars(val))
        env[key] = val
    return env


def load_client_notes(client_path: Path) -> dict[str, Any]:
    """Read sibling notes.json next to a client JSON.

    Schema (all keys optional):
      {
        "general": "free-text client-level notes",
        "sites": {
          "<domain>": {
            "notes": "...",
            "skip_items": [
              {"type": "plugin"|"theme", "slug": "...", "reason": "..."}
            ]
          }
        }
      }
    Returns {} when the file is missing or unparseable. Domain keys are
    matched leniently (lowercase, no scheme/trailing slash) at lookup time.
    """
    notes_path = client_path.parent / "notes.json"
    if not notes_path.exists():
        return {}
    try:
        return json.loads(notes_path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _normalize_domain(value: str) -> str:
    s = (value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if s.startswith("www."):
        s = s[4:]
    return s.split("/", 1)[0].rstrip(".")


def skip_items_for_domain(notes: dict[str, Any], domain: str) -> list[dict[str, Any]]:
    """Pull the skip_items list for the given domain from a notes dict."""
    sites = notes.get("sites") if isinstance(notes, dict) else None
    if not isinstance(sites, dict):
        return []
    target = _normalize_domain(domain)
    for key, entry in sites.items():
        if _normalize_domain(str(key)) != target:
            continue
        if not isinstance(entry, dict):
            return []
        items = entry.get("skip_items") or []
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            t = str(it.get("type") or "").strip().lower()
            slug = str(it.get("slug") or "").strip()
            if t in ("plugin", "theme") and slug:
                out.append({
                    "type": t, "slug": slug,
                    "reason": str(it.get("reason") or "").strip(),
                })
        return out
    return []


def resolve(raw: str | None, env: dict[str, str]) -> str:
    """Resolve $VAR placeholders against the loaded env dict."""
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("$"):
        return env.get(raw[1:], "")
    return raw


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"


# ---------------------------------------------------------------------------
# Logger setup
# ---------------------------------------------------------------------------

def make_logger(log_dir: Path, run_id: str, stream: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("wp-update")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%dT%H:%M:%SZ"
    )
    # Force UTC for log timestamps
    fmt.converter = time.gmtime

    fh = logging.FileHandler(log_dir / f"wp-update-{run_id}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)

    # --stream: show DEBUG on stdout (tail -f style, everything including
    # remote SSH commands and their output).  Default: INFO only.
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG if stream else logging.INFO)
    logger.addHandler(sh)

    return logger


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

class WPUpdater:
    """
    Orchestrates the full update lifecycle for every client application:
      1. Load inventory
      2. SSH preflight
      3. Collect baseline
      4. Create pre-flight backup
      5. Update core → themes → plugins (atomic, sequential)
      6. Verify after each step
      7. Rollback on failure
      8. Write credential-safe summary
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.env = load_env(args.env_file)
        if args.ssh_key:
            self.env["SSH_KEY"] = str(args.ssh_key)
        self.log = make_logger(args.log_dir, self.run_id, stream=args.stream)
        self.reports: list[SiteReport] = []
        self._consecutive_execute_failures = 0
        self._run_abort_reason = ""
        self._db = self._open_db()
        self._recent_successes: set[str] = self._load_recent_successes()
        if self._recent_successes:
            self.log.info(
                "Dedupe active: %d domain(s) already succeeded within last %dh — will be skipped",
                len(self._recent_successes), self.args.skip_recent,
            )

        # Global SSH credentials from .env
        self._ssh_user = self.env.get("SSH_USER", "")
        self._ssh_key = self.env.get("SSH_KEY", "")
        self._app_pw = self.env.get("APP_PW", "")

        # Build effective tier-1 candidate list: SSH_USER (back-compat)
        # followed by any entries in SSH_USER_CANDIDATES, trimmed and
        # de-duplicated while preserving order. Cloudways apps are
        # provisioned with per-app users like wpupdates, wpupdates-stage,
        # wpupdates-2 — a single SSH_USER can't cover all sites.
        self._ssh_user_candidates: list[str] = []
        _seen: set[str] = set()
        if self._ssh_user and self._ssh_user not in _seen:
            self._ssh_user_candidates.append(self._ssh_user)
            _seen.add(self._ssh_user)
        for raw in self.env.get("SSH_USER_CANDIDATES", "").split(","):
            name = raw.strip()
            if name and name not in _seen:
                self._ssh_user_candidates.append(name)
                _seen.add(name)

        # SSL context for HTTP verification
        self._ssl_ctx = ssl.create_default_context()
        if args.skip_ssl_verify:
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE

        # Pre-flight validation. load_env already expands ~ in SSH_KEY.
        if args.execute and not self._ssh_user_candidates:
            self.log.error(
                "SSH_USER or SSH_USER_CANDIDATES is required in .env for --execute mode"
            )
            raise SystemExit(1)
        key_path = Path(self._ssh_key) if self._ssh_key else None
        if key_path and not key_path.exists():
            self.log.error("SSH_KEY points to missing file: %s", key_path)
            raise SystemExit(1)
        if args.execute and not key_path and not self._app_pw:
            self.log.error("Either SSH_KEY or APP_PW must be set in .env for --execute")
            raise SystemExit(1)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> int:
        files = self._gather_client_files()
        if not files:
            self.log.error("No client JSON files found in %s", self.args.clients_dir)
            return 1

        self.log.info("=" * 70)
        self.log.info("WordPress Maintenance Run  |  ID: %s", self.run_id)
        self.log.info("Mode: %s  |  Clients: %d files",
                       "EXECUTE" if self.args.execute else "DRY-RUN", len(files))
        self.log.info("=" * 70)

        try:
            for path in files:
                self._process_client_file(path)
                if self._run_abort_reason:
                    self.log.error("ABORTING RUN  |  %s", self._run_abort_reason)
                    break
        except RollbackFailed as exc:
            self._run_abort_reason = str(exc)
            self.log.error("ABORTING RUN  |  %s", exc)

        self._write_summary()
        self._print_final_report()

        failures = [r for r in self.reports if r.overall in ("failed",)]
        return 1 if failures or self._run_abort_reason else 0

    # ------------------------------------------------------------------
    # Client file handling (graceful on incomplete files)
    # ------------------------------------------------------------------

    def _gather_client_files(self) -> list[Path]:
        if self.args.client_file:
            raw_files = self.args.client_file
            if isinstance(raw_files, (str, Path)):
                raw_files = [raw_files]
            paths: list[Path] = []
            seen: set[Path] = set()
            for raw in raw_files:
                p = Path(raw).resolve()
                if not p.exists():
                    self.log.error("Client file not found: %s", p)
                    continue
                if p in seen:
                    continue
                seen.add(p)
                paths.append(p)
            return paths
        # Supports both the legacy flat layout (clients/<slug>_cloudways.json)
        # and the per-provider/per-client subdir layout
        # (clients/cloudways/<base>/<slug>_cloudways.json).
        base = self.args.clients_dir
        found: set[Path] = set()
        for pattern in (
            "*_cloudways.json",
            "*/*_cloudways.json",
            "*/*/*_cloudways.json",
        ):
            for path in base.glob(pattern):
                found.add(path.resolve())
        return sorted(found)

    def _open_db(self) -> Any:
        """Open the SQLite history DB. Returns None on any failure or
        when --no-db is set; callers must tolerate that."""
        if getattr(self.args, "no_db", False):
            return None
        try:
            import db as _db
            self.args.db_path.parent.mkdir(parents=True, exist_ok=True)
            return _db.open_db(self.args.db_path)
        except Exception as exc:  # pragma: no cover - defensive
            self.log.warning("DB unavailable (%s) — proceeding without history", exc)
            return None

    def _load_recent_successes(self) -> set[str]:
        """Return domains whose execute-mode runs succeeded within --skip-recent hours.

        Prefers the SQLite DB (db/wpmaint.db) and falls back to scanning
        logs/wp-update-summary-*.json when the DB is unavailable or empty.
        Failed/rolled-back/skipped entries are NOT included; dry-run
        summaries are ignored.
        """
        hours = getattr(self.args, "skip_recent", 0) or 0
        if hours <= 0:
            return set()
        if self._db is not None:
            try:
                import db as _db
                domains = _db.recent_successful_domains(self._db, hours)
                if domains:
                    return domains
            except Exception as exc:  # pragma: no cover - defensive
                self.log.warning("DB dedupe query failed (%s) — falling back to logs", exc)
        # Log-scan fallback (covers fresh DB / pre-ingest history)
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        domains: set[str] = set()
        for path in sorted(self.args.log_dir.glob("wp-update-summary-*.json")):
            stem_ts = path.stem.replace("wp-update-summary-", "", 1)
            try:
                run_dt = datetime.strptime(
                    stem_ts, "%Y%m%dT%H%M%SZ"
                ).replace(tzinfo=UTC)
            except ValueError:
                continue
            if run_dt < cutoff:
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("mode") != "execute":
                continue
            for entry in data.get("sites", []):
                if entry.get("overall") == "success" and entry.get("domain"):
                    domains.add(entry["domain"])
        return domains

    def _process_client_file(self, path: Path) -> None:
        """
        Load one client JSON, extract applications, and process each.
        Incomplete or malformed files are logged and skipped — they never
        crash the entire run.
        """
        self.log.info("-" * 50)
        self.log.info("Loading client file: %s", path.name)

        try:
            doc = json.loads(path.read_text())
        except FileNotFoundError:
            self.log.warning(
                "SKIP  %s — file disappeared between gather and read", path.name
            )
            return
        except (OSError, json.JSONDecodeError) as exc:
            self.log.warning("SKIP  %s — unreadable: %s", path.name, exc)
            return

        # Validate required top-level fields
        client_name = doc.get("client_name", "")
        server_ip = doc.get("server_ip_address", "")
        apps = doc.get("applications")

        missing = []
        if not client_name or client_name.startswith("["):
            missing.append("client_name")
        if not server_ip or server_ip.startswith("["):
            missing.append("server_ip_address")
        if not isinstance(apps, list) or not apps:
            missing.append("applications")

        if missing:
            self.log.warning(
                "SKIP  %s — incomplete (missing: %s)", path.name, ", ".join(missing)
            )
            return

        # Validate all apps first, then sort staging before production.
        # In execute mode, staging sites are updated first so the operator
        # can review the logs before production sites are touched.
        notes = load_client_notes(path)
        validated: list[tuple[int, dict[str, Any], SiteReport]] = []
        for idx, app in enumerate(apps, 1):
            try:
                report = self._validate_app(doc, app, idx, path.name)
            except InventoryError as exc:
                self.log.warning(
                    "SKIP  %s app #%d — %s", path.name, idx, exc
                )
                continue
            report.skip_items = skip_items_for_domain(notes, report.domain)
            if report.skip_items:
                self.log.info(
                    "  notes.json: %d configured skip(s) for %s — %s",
                    len(report.skip_items), report.domain,
                    ", ".join(f"{i['type']}:{i['slug']}" for i in report.skip_items),
                )
            validated.append((idx, app, report))

        # Sort: staging sites first (is_staging=True sorts before False
        # when using not-is_staging as key, so staging comes first)
        if self.args.execute:
            validated.sort(key=lambda x: (not x[2].is_staging, x[0]))
            staging = [v for v in validated if v[2].is_staging]
            production = [v for v in validated if not v[2].is_staging]
            if staging and production:
                self.log.info(
                    "Staging-first: %d staging site(s) will be updated "
                    "before %d production site(s)",
                    len(staging), len(production),
                )

        for _idx, _app, report in validated:
            if report.domain in self._recent_successes:
                report.overall = "skipped"
                report.failure_detail = (
                    f"already succeeded within last {self.args.skip_recent}h"
                    " (--skip-recent dedupe)"
                )
                self._record_step(
                    report, "dedupe", "skipped",
                    f"recent successful run within {self.args.skip_recent}h",
                )
                self.log.info(
                    "SKIP  %s — already succeeded within last %dh",
                    report.domain, self.args.skip_recent,
                )
                self.reports.append(report)
                continue
            self.reports.append(report)
            self._process_site(report)

            # In execute mode, if a staging site failed or rolled back,
            # skip production sites on the same server — don't risk it.
            stop_client_file = False
            if (self.args.execute
                    and report.is_staging
                    and report.overall in ("failed", "rolled-back")):
                self._skip_remaining_production(validated, report)
                stop_client_file = True

            self._note_execute_outcome(report)
            if stop_client_file or self._run_abort_reason:
                break  # Stop processing this client file

    def _skip_remaining_production(
        self,
        validated: list[tuple[int, dict[str, Any], SiteReport]],
        failed_staging: SiteReport,
    ) -> None:
        """Mark every production SiteReport in `validated` that has not yet
        been processed as 'skipped' and append it to self.reports.

        Identity-based set difference (id()) makes the not-yet-processed
        check obviously correct and rules out double-appending if logic
        elsewhere changes which reports are recorded.
        """
        processed = {id(r) for r in self.reports}
        remaining_prod = [
            (i, a, rpt) for i, a, rpt in validated
            if not rpt.is_staging and id(rpt) not in processed
        ]
        if not remaining_prod:
            return

        self.log.warning(
            "⚠ Staging site %s %s — skipping %d production site(s) on this "
            "server",
            failed_staging.domain, failed_staging.overall, len(remaining_prod),
        )
        for _, _, prod_report in remaining_prod:
            prod_report.overall = "skipped"
            prod_report.failure_detail = (
                f"Skipped: staging site {failed_staging.domain} "
                f"{failed_staging.overall}"
            )
            self._record_step(
                prod_report, "staging-gate", "skipped",
                f"staging {failed_staging.domain} {failed_staging.overall} — "
                "not safe to proceed",
            )
            self.reports.append(prod_report)

    def _note_execute_outcome(self, r: SiteReport) -> None:
        """Track execute-mode failure streaks and open the run circuit if needed."""
        if not self.args.execute or self.args.max_consecutive_failures <= 0:
            return

        if r.overall in ("failed", "rolled-back"):
            self._consecutive_execute_failures += 1
            if self._consecutive_execute_failures >= self.args.max_consecutive_failures:
                self._run_abort_reason = (
                    "circuit breaker opened after "
                    f"{self._consecutive_execute_failures} consecutive "
                    "failed/rolled-back site(s)"
                )
                self.log.error(
                    "⚠ RUN CIRCUIT OPEN  |  %s  |  last site=%s",
                    self._run_abort_reason, r.domain,
                )
            return

        if r.overall == "success" and self._consecutive_execute_failures:
            self.log.info(
                "Run failure streak reset after success  |  %s", r.domain
            )
            self._consecutive_execute_failures = 0

    def _validate_app(
        self, doc: dict, app: dict, idx: int, filename: str
    ) -> SiteReport:
        """
        Validate a single application block and build a SiteReport.
        Raises InventoryError on any missing or invalid field.
        """
        domain = app.get("website_domain", "")
        wp_path = app.get("path_to_public_html", "")
        sftp = app.get("sftp_credentials", {})
        flags = app.get("environment_flags", {})

        if not domain or domain.startswith("["):
            raise InventoryError("missing website_domain")
        if not VALID_PATH.match(wp_path):
            raise InventoryError(f"invalid path_to_public_html: {wp_path!r}")
        if not isinstance(sftp, dict):
            raise InventoryError("sftp_credentials is not an object")
        if not isinstance(flags, dict):
            raise InventoryError("environment_flags is not an object")

        master_creds = doc.get("master_credentials", {})

        return SiteReport(
            client=doc["client_name"],
            domain=domain,
            server_ip=doc["server_ip_address"],
            wp_path=wp_path,
            is_staging=bool(flags.get("is_staging", False)),
            has_woocommerce=bool(flags.get("has_woocommerce", False)),
            ssh_user=resolve(sftp.get("username"), self.env) or self._ssh_user,
            ssh_password=resolve(sftp.get("password"), self.env) or self._app_pw,
            ssh_key_path=resolve(sftp.get("ssh_key"), self.env) or self._ssh_key,
            master_user=master_creds.get("username", ""),
            master_password=master_creds.get("password", ""),
        )

    # ------------------------------------------------------------------
    # Per-site processing
    # ------------------------------------------------------------------

    def _process_site(self, r: SiteReport) -> None:
        self.log.info(
            "Processing  %s  |  %s  |  %s", r.client, r.domain, r.wp_path
        )

        # --- WooCommerce gate ---
        if r.has_woocommerce and not self.args.include_woocommerce:
            self.log.warning(
                "⚠ WOOCOMMERCE — MANUAL REVIEW  |  %s  |  Skipped (use "
                "--include-woocommerce to override)", r.domain
            )
            self._record_step(r, "woocommerce-gate", "skipped",
                              "WooCommerce site — flagged for manual review")
            r.overall = "skipped"
            return

        # --- Staging gate ---
        if r.is_staging and self.args.skip_staging:
            self.log.info("SKIP  %s — staging site", r.domain)
            self._record_step(r, "staging-gate", "skipped", "staging site skipped")
            r.overall = "skipped"
            return

        # Track current step so failures always report the exact point
        current_step = "ssh-preflight"
        try:
            self._step_ssh_preflight(r)

            current_step = "baseline"
            self._step_collect_baseline(r)

            current_step = "disk-check"
            self._step_disk_check(r)

            current_step = "backup"
            self._step_backup(r)

            if not self.args.execute:
                r.overall = "dry-run"
                r.baseline["confidence"] = self._compute_confidence(r)
                self._print_site_report(r)
                return

            # --- Capture ownership BEFORE any mutations ---
            # When running as master user, WP-CLI will change file ownership.
            # We capture the original user:group here so we can restore it
            # after updates (and after rollback if needed).
            current_step = "capture-ownership"
            self._step_capture_ownership(r)

            # --- WooCommerce maintenance mode ---
            # Wrap the mutating section in try/finally so maint-mode is
            # ALWAYS deactivated on the way out — including when an
            # update step raises. Without this, a mid-update exception
            # would leave the site in maintenance mode until rollback's
            # own deactivate ran (or never, on the no-rollback paths).
            maint_mode_on = False
            if r.has_woocommerce:
                current_step = "woocommerce-maintenance-on"
                self._wp(r, "maintenance-mode activate")
                maint_mode_on = True
                self.log.info("Maintenance mode ON  |  %s", r.domain)

            try:
                current_step = "core-update"
                self._step_update_core(r)

                current_step = "theme-update"
                self._step_update_themes(r)

                current_step = "plugin-update"
                self._step_update_plugins(r)

                # --- Restore ownership if running as master user ---
                if r.auth_method in ("master", "master-key") and r.original_owner:
                    current_step = "restore-ownership"
                    self._step_restore_ownership(r)
            finally:
                # Deactivate maint-mode BEFORE final-verification (so the
                # site is live when we test it) and on every exception
                # path (so rollback / failure paths don't leave the site
                # stuck on the 503 maintenance page). WP-CLI updates may
                # have already toggled it off internally — a redundant
                # deactivate is benign, suppress.
                if maint_mode_on:
                    try:
                        self._wp(r, "maintenance-mode deactivate")
                        self.log.info("Maintenance mode OFF  |  %s", r.domain)
                    except (SSHError, WPCliError) as exc:
                        # Don't re-raise: a redundant deactivate after wp-cli
                        # already toggled it off is the common benign case.
                        # But log so a real PHP fatal that prevents wp-cli
                        # from booting isn't completely invisible —
                        # final-verification will still catch it.
                        self.log.warning(
                            "Maintenance mode deactivate failed (continuing to verify): "
                            "%s  |  %s", exc, r.domain,
                        )

            # --- Final verification ---
            current_step = "final-verification"
            self._verify(r)
            self._record_step(r, "final-verification", "success",
                              "site healthy after all updates")

            r.overall = "success"
            self.log.info("✓ SUCCESS  |  %s", r.domain)

            if r.backup_dir:
                with contextlib.suppress(SSHError, OSError, subprocess.SubprocessError):
                    self._ssh(r, f"rm -rf {shlex.quote(r.backup_dir)}\n")
                    self.log.info("Backup removed  |  %s  |  %s", r.domain, r.backup_dir)
                    r.backup_dir = ""

        except RollbackFailed:
            # Rollback machinery already recorded the failure on r; bubble
            # up so the operator is forced to look at it.
            raise
        except (SSHError, HealthCheckError, WPCliError) as exc:
            # Use the tracked step name — falls back to last recorded step
            r.failure_step = current_step
            r.failure_detail = str(exc)
            self.log.error(
                "✗ FAILED  |  %s  |  step=%s  |  %s",
                r.domain, r.failure_step, exc
            )

            if self.args.execute and r.backup_dir:
                self._step_rollback(r)
            else:
                r.overall = "failed"
        except (OSError, subprocess.SubprocessError) as exc:
            # Operational failure outside the typed-exception hierarchy —
            # transient DNS, disk full, broken pipe, subprocess crash, etc.
            # Don't let one site's environmental hiccup tear down the whole
            # run. Programming bugs (TypeError, AttributeError, KeyError, …)
            # are deliberately NOT caught here — they should fast-fail so
            # they're noticed and fixed.
            r.failure_step = current_step
            r.failure_detail = f"unexpected {type(exc).__name__}: {exc}"
            self.log.exception(
                "✗ UNEXPECTED  |  %s  |  step=%s  |  %s",
                r.domain, r.failure_step, exc,
            )
            if self.args.execute and r.backup_dir:
                self._step_rollback(r)
            else:
                r.overall = "failed"

    # ------------------------------------------------------------------
    # Step: SSH preflight
    # ------------------------------------------------------------------

    @staticmethod
    def _is_permission_denied(stderr: str) -> bool:
        """
        Heuristic to distinguish 'this username isn't authorized here'
        (try next candidate) from 'host is unreachable' (stop trying).

        True when the error looks like an auth failure — Cloudways returns
        either 'Permission denied' or drops the connection with
        'Received disconnect' / exit 255 + 'publickey' mention. Anything
        else (timeout, network unreachable, host key mismatch) is treated
        as fatal.
        """
        if not stderr:
            return False
        low = stderr.lower()
        return "permission denied" in low or "received disconnect" in low

    def _step_ssh_preflight(self, r: SiteReport) -> None:
        """
        Establish SSH connectivity and verify WordPress is installed.

        Three-tier auth cascade:
          1. SSH key + each candidate app-scoped user (wpupdates,
             wpupdates-stage, wpupdates-2, ...) until one succeeds.
          2. SSH key + master username — same key, but the master user has
             server-wide access to all application directories.
          3. sshpass + master password — last resort when the key isn't
             authorized for the master user.

        When master fallback is used (tier 2 or 3), r.auth_method is set to
        "master" so downstream steps know to capture and restore file
        ownership after mutations. r.auth_user is always populated with the
        username that actually authenticated.
        """
        t0 = ts()
        r.auth_method = "key"

        # --- Tier 1: SSH key + app-scoped candidates ---
        #
        # Build the per-site candidate list. If the client JSON recorded a
        # non-placeholder username for this app, prefer it first (a stale
        # value won't break the run because we still fall through to the
        # global list). r.ssh_user comes from resolve(sftp["username"]) or
        # the first global candidate — see _validate_app.
        candidates: list[str] = []
        seen: set[str] = set()
        if r.ssh_user and r.ssh_user not in seen:
            candidates.append(r.ssh_user)
            seen.add(r.ssh_user)
        for name in self._ssh_user_candidates:
            if name and name not in seen:
                candidates.append(name)
                seen.add(name)

        tier1_permission_failure = False
        for candidate in candidates:
            r.ssh_user = candidate  # _ssh / _wp read this
            try:
                self._ssh(r, "echo 'ssh-ok'")
                self._wp(r, "core is-installed")
                r.auth_user = candidate
                self.log.info(
                    "SSH tier 1 ok as %s (auth=key) | %s", candidate, r.domain
                )
                self._record_step(
                    r, "ssh-preflight", "success",
                    f"SSH reachable at {r.server_ip} as {candidate} (auth=key)", t0,
                )
                return
            except SSHError as exc:
                if self._is_permission_denied(str(exc)):
                    self.log.debug(
                        "Tier 1 candidate %s denied; trying next", candidate
                    )
                    tier1_permission_failure = True
                    continue
                raise  # Network / timeout / host unreachable — don't waste time

        if candidates and not tier1_permission_failure:
            # No candidates ever hit a permission error but none succeeded
            # either — this means the list was empty. Guarded below.
            pass

        # Need master credentials for tier 2 and 3
        if not r.master_user:
            raise SSHError(
                f"Permission denied on {r.wp_path} and no master "
                f"credentials available for fallback"
            )

        # --- Tier 2: SSH key + master username ---
        self.log.info(
            "SSH tier 1 failed for all candidates %s — trying key+master user | %s",
            candidates, r.domain,
        )
        r.auth_method = "master-key"
        r.auth_user = r.master_user
        try:
            self._ssh(r, "echo 'ssh-ok'")
            self._wp(r, "core is-installed")
            self._record_step(r, "ssh-preflight", "success",
                              f"SSH reachable at {r.server_ip} (auth=master-key)", t0)
            return
        except SSHError:
            pass  # Fall through to tier 3

        # --- Tier 3: sshpass + master password ---
        if not r.master_password:
            raise SSHError(
                f"Key auth failed for both wpupdates and master user on "
                f"{r.server_ip}, and no master password available"
            )
        if not shutil.which("sshpass"):
            raise SSHError(
                "Key auth failed — master password fallback requires "
                "sshpass but it's not installed"
            )

        self.log.info(
            "Key+master failed — trying sshpass+master password for %s",
            r.wp_path,
        )
        r.auth_method = "master"
        r.auth_user = r.master_user
        self._ssh(r, "echo 'ssh-ok'")
        self._wp(r, "core is-installed")
        self._record_step(r, "ssh-preflight", "success",
                          f"SSH reachable at {r.server_ip} (auth=master-password)", t0)

    # ------------------------------------------------------------------
    # Step: Capture ownership
    #
    # When running as master user, WP-CLI changes file ownership to
    # master_xxx:master_xxx.  We capture the original user:group of the
    # WordPress directory BEFORE any mutations so we can chown -R back
    # after updates complete (or after a rollback).
    #
    # This is a no-op when running with the app-scoped SSH key, since
    # that user already owns the files.
    # ------------------------------------------------------------------

    def _step_capture_ownership(self, r: SiteReport) -> None:
        t0 = ts()
        # stat -c '%U:%G' returns "username:groupname" of the directory
        raw = self._ssh(r, f"stat -c '%U:%G' {shlex.quote(r.wp_path)}").strip()
        if ":" in raw:
            r.original_owner = raw
            self._record_step(r, "capture-ownership", "success",
                              f"owner={raw} (auth={r.auth_method})", t0)
            self.log.info("Captured ownership  |  %s  |  %s", r.domain, raw)
        else:
            # Couldn't parse — record but don't block
            self._record_step(r, "capture-ownership", "success",
                              f"could not parse ownership (raw={raw!r}), "
                              "will skip restore", t0)

    # ------------------------------------------------------------------
    # Step: Restore ownership
    #
    # After updates or rollback, restore the original user:group on all
    # files under public_html.  Only runs when auth_method="master".
    # ------------------------------------------------------------------

    def _step_restore_ownership(self, r: SiteReport) -> None:
        if not r.original_owner or ":" not in r.original_owner:
            return
        t0 = ts()
        owner = r.original_owner
        script = f"chown -R {shlex.quote(owner)} {shlex.quote(r.wp_path)}"
        self._ssh(r, script, timeout=self.args.remote_timeout)
        self._record_step(r, "restore-ownership", "success",
                          f"chown -R {owner} on {r.wp_path}", t0)
        self.log.info("Restored ownership  |  %s  |  %s", r.domain, owner)

    # ------------------------------------------------------------------
    # Step: Baseline collection
    # ------------------------------------------------------------------

    def _step_collect_baseline(self, r: SiteReport) -> None:
        t0 = ts()

        plugins = self._wp_json(r, "plugin list --format=json")
        themes = self._wp_json(r, "theme list --format=json")
        core_updates = self._wp_json(r, "core check-update --format=json",
                                     allow_empty=True)

        # Detect known backup/migration plugins already installed
        backup_plugins = []
        for p in plugins:
            slug = p.get("name", "")
            if slug in KNOWN_BACKUP_PLUGINS:
                backup_plugins.append({
                    "slug": slug,
                    "label": KNOWN_BACKUP_PLUGINS[slug],
                    "status": p.get("status", "unknown"),
                    "version": p.get("version", "?"),
                })

        r.baseline = {
            "wp_version": self._wp_text(r, "core version"),
            "php_version": self._wp_text(r, "eval 'echo PHP_VERSION;'"),
            "siteurl": self._wp_text(r, "option get siteurl"),
            "plugins": plugins,
            "themes": themes,
            "core_updates": core_updates,
            "plugin_updates": [p for p in plugins if p.get("update") == "available"],
            "theme_updates": [t for t in themes if t.get("update") == "available"],
            "backup_plugins": backup_plugins,
        }

        self._record_step(
            r, "baseline", "success",
            f"WP {r.baseline['wp_version']}  |  "
            f"{len(r.baseline['plugin_updates'])} plugin updates  |  "
            f"{len(r.baseline['theme_updates'])} theme updates",
            t0,
        )

    # ------------------------------------------------------------------
    # Step: Pre-flight backup
    #
    # Backups go to <app_dir>/private_html/wp-maintenance-backups/<run_id>/
    # private_html is group-writable by www-data (same group as the app SSH
    # user), not web-accessible, and persistent across reboots.
    # ------------------------------------------------------------------

    def _step_disk_check(self, r: SiteReport) -> None:
        """
        Check available disk space and estimate backup size BEFORE writing
        anything.  A WordPress backup needs room for:
          - A compressed tar of public_html
          - A full SQL dump of the database
        We estimate the backup at ~50% of public_html size (tar.gz compression)
        plus a generous margin.  If available space is less than 2x the
        estimated backup size, we abort — filling a disk on a shared Cloudways
        server could take down every app on that instance.

        This check runs in both dry-run and execute mode so the operator
        always sees the disk health.
        """
        t0 = ts()

        # du -sb = total bytes of public_html
        # df -B1 = available bytes on the partition
        check_script = f"""\
du_bytes=$(du -sb {shlex.quote(r.wp_path)} 2>/dev/null | awk '{{print $1}}')
avail_bytes=$(df -B1 {shlex.quote(r.wp_path)} 2>/dev/null | awk 'NR==2{{print $4}}')
echo "${{du_bytes:-0}} ${{avail_bytes:-0}}"
"""
        raw = self._ssh(r, check_script).strip()
        try:
            site_bytes, avail_bytes = (int(p) for p in raw.split())
        except ValueError:
            self._record_step(r, "disk-check", "success",
                              f"could not parse disk info (raw={raw!r}), proceeding", t0)
            return

        site_mb = site_bytes / (1024 * 1024)
        avail_mb = avail_bytes / (1024 * 1024)
        # Estimate: compressed tar ≈ 50% of original + SQL dump ≈ 10% of original
        est_backup_mb = site_mb * 0.6
        # Require at least 2x the estimated backup size as headroom
        required_mb = est_backup_mb * 2

        r.baseline["disk"] = {
            "site_mb": round(site_mb, 1),
            "available_mb": round(avail_mb, 1),
            "estimated_backup_mb": round(est_backup_mb, 1),
        }

        if avail_mb < required_mb:
            detail = (
                f"INSUFFICIENT DISK — site={site_mb:.0f}MB, "
                f"available={avail_mb:.0f}MB, need≥{required_mb:.0f}MB"
            )
            self.log.error("⚠ %s  |  %s", detail, r.domain)
            self._record_step(r, "disk-check", "failed", detail, t0)
            raise HealthCheckError(detail)

        detail = (
            f"site={site_mb:.0f}MB, available={avail_mb:.0f}MB, "
            f"est_backup={est_backup_mb:.0f}MB — OK"
        )
        self._record_step(r, "disk-check", "success", detail, t0)

    def _step_backup(self, r: SiteReport) -> None:
        t0 = ts()

        # Extract the application hash from the path:
        # /home/master/applications/<hash>/public_html → <hash>
        app_hash = r.wp_path.split("/")[-2]
        backup_dir = (
            f"/home/master/applications/{app_hash}/private_html"
            f"/wp-maintenance-backups/{self.run_id}"
        )
        r.backup_dir = backup_dir

        if not self.args.execute:
            self._record_step(r, "backup", "planned",
                              f"would create backup at {backup_dir}", t0)
            return

        # The backup script is piped via stdin to avoid quoting issues.
        # It creates:
        #   preflight.sql       — full DB dump with DROP TABLE statements
        #   public_html.tar.gz  — compressed snapshot of the entire app
        #   plugins.json        — plugin inventory at backup time
        #   themes.json         — theme inventory at backup time
        script = f"""\
set -euo pipefail
cd {shlex.quote(r.wp_path)}
mkdir -p {shlex.quote(backup_dir)}
wp --path={shlex.quote(r.wp_path)} db export {shlex.quote(backup_dir + '/preflight.sql')} --add-drop-table 2>&1
wp --path={shlex.quote(r.wp_path)} plugin list --format=json > {shlex.quote(backup_dir + '/plugins.json')} 2>&1
wp --path={shlex.quote(r.wp_path)} theme list --format=json > {shlex.quote(backup_dir + '/themes.json')} 2>&1
# tar may exit 1 on benign warnings ("file changed as we read it" on live sites
# under low write load — Breeze cache, session files, etc.). Tolerate exit ≤1
# but fail on >1. The `|| _tar_rc=$?` is required: without it, `set -e` fires
# on tar's non-zero exit before the rc capture runs, killing the script.
_tar_rc=0
tar -czf {shlex.quote(backup_dir + '/public_html.tar.gz')} -C {shlex.quote(r.wp_path)} . 2>&1 || _tar_rc=$?
[ "$_tar_rc" -le 1 ] || exit "$_tar_rc"
# Verify both backup files are non-empty
test -s {shlex.quote(backup_dir + '/preflight.sql')}
test -s {shlex.quote(backup_dir + '/public_html.tar.gz')}
# Verify archive is readable and contains wp-content/ (guards against partial writes).
# Run in a subshell with pipefail disabled: grep -q exits on first match sending SIGPIPE
# to tar, which would otherwise cause pipefail to report a false failure.
(set +o pipefail; tar -tzf {shlex.quote(backup_dir + '/public_html.tar.gz')} 2>/dev/null | grep -qE '(^|/)wp-content/') || {{ echo 'backup-integrity-fail: wp-content/ missing from archive'; exit 1; }}
echo 'backup-ok'
"""
        self._ssh(r, script, timeout=self.args.remote_timeout)
        self._record_step(r, "backup", "success",
                          f"backup at {backup_dir}", t0)

    # ------------------------------------------------------------------
    # Step: Update WordPress core
    # ------------------------------------------------------------------

    def _step_update_core(self, r: SiteReport) -> None:
        if not r.baseline.get("core_updates"):
            self._record_step(r, "core-update", "success",
                              "no core updates pending")
            return

        t0 = ts()
        old_version = r.baseline.get("wp_version", "?")
        self._wp(r, "core update", timeout=self.args.remote_timeout)
        self._wp(r, "core update-db", timeout=self.args.remote_timeout)
        self._verify(r)
        # Refresh baseline so summaries report the post-update version.
        # A transient read failure here shouldn't undo a successful update.
        with contextlib.suppress(SSHError):
            r.baseline["wp_version"] = self._wp_text(r, "core version")
        self._record_step(r, "core-update", "success",
                          f"core {old_version} → {r.baseline.get('wp_version', '?')}", t0)

    # ------------------------------------------------------------------
    # Step: Update themes (sequential, one-by-one)
    # ------------------------------------------------------------------

    def _step_update_themes(self, r: SiteReport) -> None:
        updates = r.baseline.get("theme_updates", [])
        skip_map = {
            i["slug"]: i.get("reason", "")
            for i in r.skip_items if i.get("type") == "theme"
        }
        if not updates:
            self._record_step(r, "theme-update", "success",
                              "no theme updates pending")
            return

        for theme in updates:
            slug = theme.get("name", "").strip()
            ver_from = theme.get("version", "?")
            ver_to = theme.get("update_version", "?")
            if not slug:
                continue
            if slug in skip_map:
                reason = skip_map[slug] or "configured in notes.json"
                self._record_step(
                    r, f"theme-update:{slug}", "skipped",
                    f"{slug} {ver_from}→{ver_to} configured skip: {reason}",
                )
                self.log.info(
                    "  ⤼ Skipping theme  %s  (configured: %s)  |  %s",
                    slug, reason, r.domain,
                )
                continue
            step = f"theme-update:{slug}"
            t0 = ts()
            try:
                self._wp(r, f"theme update {shlex.quote(slug)}",
                         timeout=self.args.remote_timeout)
                self._verify(r)
            except (SSHError, HealthCheckError) as exc:
                self._record_step(r, step, "failed",
                                  f"{slug} {ver_from}→{ver_to} FAILED: {exc}", t0)
                raise
            self._record_step(r, step, "success",
                              f"{slug} {ver_from}→{ver_to}", t0)

    # ------------------------------------------------------------------
    # Plugin update helpers
    # ------------------------------------------------------------------

    def _run_plugin_update_structured(self, r: SiteReport, slug: str) -> dict:
        """Run `wp plugin update <slug> --format=json` and return the result dict.

        Returns a dict with at least {"name": slug, "status": "..."}.
        On SSH/WP-CLI failure: {"name": slug, "status": "Error", "_exit_nonzero": True}.
        On JSON parse failure: {"name": slug, "status": "Error", "_parse_error": True}.
        When JSON parses cleanly but no entry for the slug is present, returns
        {"name": slug, "status": "Error", "_no_entry": True} — the absence of
        the slug in the response is a signal something went wrong, NOT a
        silent "up to date".
        """
        try:
            raw = self._wp(
                r, f"plugin update {shlex.quote(slug)} --format=json",
                timeout=self.args.remote_timeout,
            )
        except (SSHError, WPCliError) as exc:
            return {"name": slug, "status": "Error", "_exit_nonzero": True, "_error": str(exc)}

        # Strip PHP warnings / notices that may appear before the JSON array
        bracket = raw.find("[")
        if bracket == -1:
            return {"name": slug, "status": "Error", "_parse_error": True, "_raw": raw}
        try:
            entries = json.loads(raw[bracket:])
        except json.JSONDecodeError:
            return {"name": slug, "status": "Error", "_parse_error": True, "_raw": raw}

        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == slug:
                return entry
        # JSON returned but no matching entry — treat as Error, NOT up to date.
        # WP-CLI normally emits an entry per slug; its absence is ambiguous
        # and the safe default is to surface it rather than silently pass.
        return {"name": slug, "status": "Error", "_no_entry": True}

    # ------------------------------------------------------------------
    # Step: Update plugins (sequential — one at a time with verification)
    #
    # Plugins are the #1 cause of site breakage during WordPress
    # maintenance.  Each plugin is updated individually, and the site is
    # health-checked (via _verify) after every single update.  The
    # classification is:
    #   * wp-cli reports success/up-to-date AND verify passes -> success
    #   * wp-cli reports a non-fatal error (license/auth/etc.) but the
    #     site still verifies -> skipped, continue to next plugin
    #   * verify FAILS after an update -> attempt `wp plugin deactivate`
    #     to isolate the offender.  If that recovers verify -> degraded
    #     (manual review required) and continue.  If it does NOT -> raise
    #     SSHError so _process_site escalates to the full-site rollback
    #     (preflight.sql + public_html.tar.gz), which is the only recovery
    #     that is safe in the presence of DB schema migrations.
    # ------------------------------------------------------------------

    def _step_update_plugins(self, r: SiteReport) -> None:
        updates = r.baseline.get("plugin_updates", [])
        skip_map = {
            i["slug"]: i.get("reason", "")
            for i in r.skip_items if i.get("type") == "plugin"
        }
        if not updates:
            self._record_step(r, "plugin-update", "success",
                              "no plugin updates pending")
            return

        for plugin in updates:
            slug = plugin.get("name", "").strip()
            ver_from = plugin.get("version", "?") or "?"
            ver_to = plugin.get("update_version", "?") or "?"
            if not slug:
                continue

            if slug in skip_map:
                reason = skip_map[slug] or "configured in notes.json"
                self._record_step(
                    r, f"plugin-update:{slug}", "skipped",
                    f"{slug} {ver_from}→{ver_to} configured skip: {reason}",
                )
                self.log.info(
                    "  ⤼ Skipping plugin  %s  (configured: %s)  |  %s",
                    slug, reason, r.domain,
                )
                continue

            step = f"plugin-update:{slug}"
            t0 = ts()
            self.log.info(
                "  Updating plugin  %s  (%s → %s)  |  %s",
                slug, ver_from, ver_to, r.domain,
            )

            result = self._run_plugin_update_structured(r, slug)
            # Retry once on transient signals: empty wp-cli output (parse
            # error with no raw text), or "no entry" responses. These have
            # been observed under WC-maintenance-mode contention and brief
            # network blips; a single retry usually resolves them.
            if result.get("_parse_error") and not (result.get("_raw") or "").strip():
                self.log.info("  ↻ Retrying plugin update (empty output)  |  %s", slug)
                result = self._run_plugin_update_structured(r, slug)
            elif result.get("_no_entry"):
                self.log.info("  ↻ Retrying plugin update (no entry)  |  %s", slug)
                result = self._run_plugin_update_structured(r, slug)
            elif result.get("_exit_nonzero"):
                # SSH transport failed or wp-cli aborted before producing
                # parseable output. Cloudways occasionally returns SSH
                # exit 255 mid-update on a slow shell; retry once before
                # treating as fatal.
                self.log.info(
                    "  ↻ Retrying plugin update (ssh/wpcli failure: %s)  |  %s",
                    result.get("_error", "?"), slug,
                )
                result = self._run_plugin_update_structured(r, slug)
            status = (result.get("status", "") or "").strip().lower()
            # Prefer the post-update version reported by wp-cli, fall back
            # to the baseline's target version.
            ver_to_reported = result.get("version") or ver_to or "?"

            # Always verify after the update attempt — this is the oracle
            # that decides whether the site still works.
            try:
                self._verify(r)
                verify_ok = True
                verify_exc: HealthCheckError | None = None
            except HealthCheckError as exc:
                verify_ok = False
                verify_exc = exc

            if verify_ok and status in _PLUGIN_STATUS_SUCCESS:
                self._record_step(r, step, "success",
                                  f"{slug} {ver_from}→{ver_to_reported}", t0)
                continue

            if verify_ok and status in _PLUGIN_STATUS_UPTODATE:
                self._record_step(r, step, "success",
                                  f"{slug} up to date", t0)
                continue

            if verify_ok:
                # wp-cli reported an Error / unknown status but the site
                # is still healthy.  Typical causes: premium license not
                # active, auth failure fetching the zip, network blip.
                # Skip and continue.
                err_msg = _extract_plugin_error(result)
                detail = f"{slug} {ver_from}→{ver_to} non-fatal error: {err_msg}"
                self._record_step(r, step, "skipped", detail, t0)
                self.log.warning(
                    "  ⚠ Skipping plugin  %s  (%s → %s): non-fatal error: %s  |  %s",
                    slug, ver_from, ver_to, err_msg, r.domain,
                )
                continue

            # verify FAILED — the update broke the site.  Attempt
            # deactivation to isolate the offending plugin before
            # escalating to full-site rollback.
            self.log.warning(
                "  ⚠ Plugin  %s  update broke site; attempting deactivation  |  %s",
                slug, r.domain,
            )
            deactivate_exit_ok = True
            try:
                self._wp(r, f"plugin deactivate {shlex.quote(slug)}",
                         timeout=self.args.remote_timeout)
            except (SSHError, WPCliError) as exc:
                deactivate_exit_ok = False
                self.log.warning(
                    "  ⚠ `wp plugin deactivate %s` failed: %s  |  %s",
                    slug, exc, r.domain,
                )

            try:
                self._verify(r)
                recovered = True
            except HealthCheckError:
                recovered = False

            if recovered:
                deact_note = "ok" if deactivate_exit_ok else "non-zero exit"
                detail = (
                    f"{slug} {ver_from}→{ver_to} fatal update broke site; "
                    f"plugin deactivated ({deact_note}) — requires manual review"
                )
                self._record_step(r, step, "degraded", detail, t0)
                self.log.warning(
                    "  ⚠ Plugin  %s  deactivated after fatal update — manual "
                    "review required  |  %s",
                    slug, r.domain,
                )
                continue

            # Deactivation did not recover the site — escalate to
            # full-site rollback via _process_site's handler.
            detail = (
                f"{slug} {ver_from}→{ver_to} fatal update broke site; "
                f"deactivation did not recover (verify: {verify_exc})"
            )
            self._record_step(r, step, "failed", detail, t0)
            raise SSHError(
                f"plugin {slug} update broke site, deactivation failed — "
                "escalating to full rollback"
            )

    # ------------------------------------------------------------------
    # Rollback
    #
    # Restore sequence:
    #   0. Backstop: refuse if live wp-content/ missing or backup tarball
    #      doesn't contain wp-content/ (corrupt/wrong archive)
    #   1. Archive the failed state (for post-mortem analysis)
    #   2. Best-effort chown so wipe can recurse into restrictive-mode dirs
    #   3. Wipe public_html contents (tolerate partial; verify-empty after)
    #   4. Extract the pre-flight tar (only if wipe completed)
    #   5. Backstop: verify wp-content/ landed
    #   6. Import the pre-flight database dump
    #   7. Verify the site is back to healthy
    #
    # If any of these steps fail, the rollback is marked as failed and
    # the operator must intervene manually.  The backup files remain on
    # disk for manual recovery.
    # ------------------------------------------------------------------

    def _step_rollback(self, r: SiteReport) -> None:
        t0 = ts()
        self.log.warning("ROLLING BACK  |  %s  |  from %s", r.domain, r.backup_dir)

        db_backup = f"{r.backup_dir}/preflight.sql"
        fs_backup = f"{r.backup_dir}/public_html.tar.gz"
        failed_snapshot = f"{r.backup_dir}/failed-state.tar.gz"

        script = f"""\
set -euo pipefail
# 0a. Defense-in-depth on the live tree: refuse to wipe a directory that
#     doesn't look like a WordPress install. VALID_PATH already gates this
#     at the Python layer; this is a backstop.
if [ ! -d {shlex.quote(r.wp_path + "/wp-content")} ]; then
    echo "rollback-abort: {r.wp_path}/wp-content not present — refusing to wipe" >&2
    exit 99
fi
# 0b. Defense-in-depth on the backup archive: refuse to extract a tarball
#     that doesn't contain wp-content/. Catches truncated/corrupt backups
#     and refuses to repopulate public_html with non-WP contents.
# Subshell with pipefail off: grep -q exits on first match and SIGPIPEs tar,
# which would otherwise make pipefail report a false integrity failure on
# large archives.
if ! (set +o pipefail; tar -tzf {shlex.quote(fs_backup)} 2>/dev/null | grep -qE '(^|/)wp-content/'); then
    echo "rollback-abort: {fs_backup} missing wp-content/ — refusing to extract" >&2
    exit 98
fi
# 1. Archive the broken state for forensic analysis
tar -czf {shlex.quote(failed_snapshot)} -C {shlex.quote(r.wp_path)} . 2>/dev/null || true
# 2. Best-effort ownership recovery before the wipe. rm -rf normally
#    succeeds via parent-dir perms regardless of child ownership, but
#    a child directory with restrictive mode can block recursion. If
#    we own the tree we can fix it; if not, this no-ops and the
#    verify-empty backstop below catches any residual files.
chown -R "$(id -u):$(id -g)" {shlex.quote(r.wp_path)} 2>/dev/null || true
# 3. Wipe current public_html contents. Tolerate partial failure; we
#    verify-empty below before any extract. If extract ran on a half-
#    wiped tree, leftover files from the failed state would mix with
#    restored files and produce a corrupted live site.
find {shlex.quote(r.wp_path)} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} + 2>&1 || true
if [ -n "$(ls -A {shlex.quote(r.wp_path)} 2>/dev/null)" ]; then
    echo "rollback-abort: could not fully wipe {r.wp_path} (residual files present); failed-state snapshot preserved at {failed_snapshot}, backup intact at {fs_backup} — manual recovery required" >&2
    exit 97
fi
# 4. Restore filesystem from pre-flight backup
tar -xzf {shlex.quote(fs_backup)} -C {shlex.quote(r.wp_path)}
# 5. Backstop: verify wp-content/ landed (catches a tar that exited 0 but
#    extracted nothing useful — vanishingly rare but the cost of checking
#    is one stat call).
if [ ! -d {shlex.quote(r.wp_path + "/wp-content")} ]; then
    echo "rollback-abort: extract completed but {r.wp_path}/wp-content/ missing — manual recovery required" >&2
    exit 96
fi
# 6. Restore database from pre-flight dump.
#    cd into wp_path so wp-config.php's relative require (e.g. require 'wp-salt.php')
#    resolves against the WP root, not the SSH login home dir.
cd {shlex.quote(r.wp_path)}
wp --path={shlex.quote(r.wp_path)} db import {shlex.quote(db_backup)} 2>&1
echo 'rollback-ok'
"""
        try:
            self._ssh(r, script, timeout=self.args.remote_timeout)
            # Restore ownership after rollback if running as master user
            if r.auth_method in ("master", "master-key") and r.original_owner:
                self._step_restore_ownership(r)
            self._verify(r)
            # Deactivate maintenance mode if it was on (best-effort).
            if r.has_woocommerce:
                with contextlib.suppress(SSHError):
                    self._wp(r, "maintenance-mode deactivate")
            r.rollback_result = "success"
            r.overall = "rolled-back"
            self._record_step(r, "rollback", "success",
                              f"restored from {r.backup_dir}", t0)
            self.log.warning("ROLLBACK OK  |  %s", r.domain)
        except (SSHError, HealthCheckError) as exc:
            r.rollback_result = f"FAILED: {exc}"
            r.overall = "failed"
            self._record_step(r, "rollback", "failed", str(exc), t0)
            self.log.error(
                "⚠ ROLLBACK FAILED  |  %s  |  %s  |  Manual recovery needed "
                "from %s", r.domain, exc, r.backup_dir
            )
            raise RollbackFailed(
                f"rollback failed for {r.domain}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Verification
    #
    # Two-layer check after every update step:
    #   1. WP-CLI: `wp core is-installed` — catches fatal PHP errors
    #   2. HTTP: GET the site + /wp-login.php — catches 5xx and crash
    #      markers in the response body
    # ------------------------------------------------------------------

    def _verify(self, r: SiteReport) -> None:
        """Raise HealthCheckError if the site is unhealthy."""
        # Layer 1: WP-CLI sanity
        self._wp(r, "core is-installed")

        # Layer 2: HTTP health
        result = self._http_check(r.domain)
        if result != "ok":
            raise HealthCheckError(result)

    # Retry transient connection errors before declaring a site unhealthy.
    # 5xx and fatal-marker matches are deterministic and never retried.
    HTTP_RETRY_BACKOFFS = (0, 1.0, 2.0)

    def _http_check(self, domain: str) -> str:
        """
        Hit the site over HTTPS (fallback to HTTP) and check for 5xx
        status codes or fatal error markers in the response body.
        Returns "ok" or a description of the problem.
        """
        schemes = (
            [domain] if domain.startswith(("http://", "https://"))
            else [f"https://{domain}", f"http://{domain}"]
        )
        last_err = "all HTTP checks failed"

        for base in schemes:
            for suffix in ("", "/wp-login.php"):
                url = f"{base}{suffix}"
                outcome = self._http_check_one(url)
                if outcome is None:
                    # Passed — check the next suffix.
                    continue
                if outcome.startswith("transient:"):
                    # Exhausted retries on a connection-class error. Move
                    # to the next scheme but remember the message.
                    last_err = outcome[len("transient:"):]
                    break
                # Definitive failure (5xx or fatal marker) — bail out.
                return outcome
            else:
                return "ok"

        return last_err

    def _http_check_one(self, url: str) -> str | None:
        """Probe a single URL with retries for transient errors.

        Returns:
            None — passed; check the next suffix on the same scheme.
            "transient:<msg>" — transient failure exhausted retries; the
                                caller should try the next scheme.
            anything else — definitive failure description; caller bails.
        """
        last_exc: Exception | None = None
        last_5xx: str | None = None
        for backoff in self.HTTP_RETRY_BACKOFFS:
            if backoff:
                time.sleep(backoff)
            try:
                req = urlrequest.Request(
                    url, headers={"User-Agent": "wp-update/1.0 (maintenance)"}
                )
                with urlrequest.urlopen(
                    req, timeout=self.args.http_timeout, context=self._ssl_ctx
                ) as resp:
                    if resp.status >= 500:
                        # Retry 5xx within this URL: WC + Breeze can
                        # serve a stale 503 for 1–3s after wp-cli
                        # internally deactivates maintenance mode.
                        last_5xx = f"{url} → HTTP {resp.status}"
                        continue
                    body = resp.read(65536).decode("utf-8", errors="ignore").lower()
                    for marker in FATAL_MARKERS:
                        if marker in body:
                            return f"{url} → fatal marker: {marker!r}"
                return None
            except urlerror.HTTPError as exc:
                if exc.code >= 500:
                    last_5xx = f"{url} → HTTP {exc.code}"
                    continue
                # 3xx/4xx are deterministic — pass and check next suffix.
                return None
            except OSError as exc:
                # Includes URLError, socket.timeout, ConnectionRefusedError,
                # name resolution failures. Worth retrying.
                last_exc = exc
                continue

        # Exhausted retries. A repeated 5xx is now definitive (caller bails).
        if last_5xx is not None:
            self.log.debug("HTTP 5xx persisted across retries: %s", last_5xx)
            return last_5xx
        # Otherwise it's a transient connection-class failure.
        msg = f"{url} → {last_exc}" if last_exc is not None else f"{url} → unknown"
        self.log.debug("HTTP transient failure: %s", msg)
        return f"transient:{msg}"

    # ------------------------------------------------------------------
    # SSH transport
    #
    # Scripts are piped via stdin to avoid SSH argument quoting bugs.
    # The remote command is always `bash -ls`:
    #   -l  login shell (loads .bashrc / .profile where wp-cli lives)
    #   -s  read commands from stdin
    #
    # Authentication priority:
    #   1. SSH key (if path exists on disk)
    #   2. sshpass + password (if sshpass is installed)
    #   3. Error
    # ------------------------------------------------------------------

    def _ssh(self, r: SiteReport, script: str, timeout: int | None = None) -> str:
        """Execute a script on the remote host via SSH stdin piping."""
        target = f"{r.ssh_user}@{r.server_ip}"
        cmd, sshpass_password = self._ssh_cmd(r)
        effective_timeout = timeout or self.args.remote_timeout

        self.log.debug("SSH → %s  |  %s", target, script.replace("\n", " \\n "))

        # Pass sshpass passwords via SSHPASS env var (`sshpass -e`) instead
        # of argv (`sshpass -p`) so they don't leak in `ps auxww` output.
        env = None
        if sshpass_password is not None:
            env = {**os.environ, "SSHPASS": sshpass_password}

        try:
            proc = subprocess.run(
                cmd,
                input=script,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise SSHError(f"SSH timeout ({effective_timeout}s) on {target}") from exc

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if stdout:
            self.log.debug("SSH ← stdout  |  %s  |  %s", target, stdout[:500])
        if stderr:
            self.log.debug("SSH ← stderr  |  %s  |  %s", target, stderr[:500])

        if proc.returncode != 0:
            raise SSHError(
                f"exit={proc.returncode} on {target}: "
                f"{stderr or stdout or 'no output'}"
            )
        return stdout

    def _ssh_cmd(self, r: SiteReport) -> tuple[list[str], str | None]:
        """Build the SSH command list and the password (if any) for sshpass.

        Returns (argv, password_for_SSHPASS_env). The caller must put the
        password in the SSHPASS env var when it's not None and use
        `sshpass -e` rather than `sshpass -p`, so the secret never appears
        in `ps`.

        Auth methods (set by _step_ssh_preflight):
          "key"        — SSH key + wpupdates user (app-scoped)
          "master-key" — SSH key + master username (server-wide)
          "master"     — sshpass + master password (last resort)
        """
        common_opts = [
            "-F", str(self.args.ssh_config),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={self.args.connect_timeout}",
        ]

        # load_env already expanded ~ in any path read from .env, so this
        # is just a typesafe Path() coercion.
        key_path = Path(r.ssh_key_path) if r.ssh_key_path else None

        # Tier 2: SSH key + master username
        if r.auth_method == "master-key":
            target = f"{r.master_user}@{r.server_ip}"
            if key_path and key_path.exists():
                return ([
                    "ssh", *common_opts, "-o", "BatchMode=yes",
                    "-i", str(key_path), target, "bash", "-ls",
                ], None)
            raise SSHError(f"master-key auth requires SSH key but {key_path} not found")

        # Tier 3: sshpass + master password (via SSHPASS env)
        if r.auth_method == "master":
            target = f"{r.master_user}@{r.server_ip}"
            return ([
                "sshpass", "-e",
                "ssh", *common_opts, target, "bash", "-ls",
            ], r.master_password)

        # Tier 1 (default): SSH key + wpupdates user
        target = f"{r.ssh_user}@{r.server_ip}"
        if key_path and key_path.exists():
            return ([
                "ssh", *common_opts, "-o", "BatchMode=yes",
                "-i", str(key_path), target, "bash", "-ls",
            ], None)

        # Password fallback for tier 1 (when no key file exists)
        if r.ssh_password and shutil.which("sshpass"):
            return ([
                "sshpass", "-e",
                "ssh", *common_opts, target, "bash", "-ls",
            ], r.ssh_password)

        raise SSHError(
            f"No SSH auth method for {target}. "
            "Set SSH_KEY in .env or install sshpass for password fallback."
        )

    def _wp(self, r: SiteReport, wp_cmd: str, timeout: int | None = None) -> str:
        """Run a wp-cli command on the remote host.

        Cloudways wp-config.php files use `require('wp-salt.php')` with a
        relative path, so PHP resolves it against the CWD — not the directory
        where wp-config.php lives.  We must `cd` into the WordPress root
        before invoking wp-cli, otherwise the require fails.
        """
        # error_reporting=5  (E_ERROR | E_PARSE) silences wp-cli's noisy
        # PHP warnings on PHP 8.x — notably the "Undefined property:
        # stdClass::$requires" warning emitted by Plugin_Command.php when
        # plugin metadata lacks a 'requires' field. Fatals still surface.
        script = (
            f"cd {shlex.quote(r.wp_path)} && "
            f"WP_CLI_CACHE_DIR=$HOME/tmp/.wp-cli-cache "
            f"WP_CLI_PHP_ARGS='-d error_reporting=5' "
            f"wp --path={shlex.quote(r.wp_path)} {wp_cmd}"
        )
        return self._ssh(r, script, timeout)

    def _wp_text(self, r: SiteReport, wp_cmd: str) -> str:
        return self._wp(r, wp_cmd).strip()

    def _wp_json(self, r: SiteReport, wp_cmd: str,
                 allow_empty: bool = False) -> list[dict]:
        raw = self._wp(r, wp_cmd).strip()
        if not raw:
            if allow_empty:
                return []
            raise WPCliError(f"Empty output from: wp {wp_cmd}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WPCliError(f"Bad JSON from `wp {wp_cmd}`: {exc}") from exc

    # ------------------------------------------------------------------
    # Step recording
    # ------------------------------------------------------------------

    def _record_step(self, r: SiteReport, name: str, status: str,
                     detail: str, started: str | None = None) -> None:
        r.steps.append(StepResult(
            name=name, status=status,
            started=started or ts(), ended=ts(), detail=detail,
        ))

    # ------------------------------------------------------------------
    # Summary output — credentials are NEVER written to disk
    # ------------------------------------------------------------------

    def _write_summary(self) -> None:
        summary = {
            "run_id": self.run_id,
            "mode": "execute" if self.args.execute else "dry-run",
            "generated_at": ts(),
            "total_sites": len(self.reports),
            "results": {
                "success": len([r for r in self.reports if r.overall == "success"]),
                "dry_run": len([r for r in self.reports if r.overall == "dry-run"]),
                "skipped": len([r for r in self.reports if r.overall == "skipped"]),
                "rolled_back": len([r for r in self.reports if r.overall == "rolled-back"]),
                "failed": len([r for r in self.reports if r.overall == "failed"]),
            },
            "sites": [r.to_dict() for r in self.reports],
        }

        path = self.args.log_dir / f"wp-update-summary-{self.run_id}.json"
        path.write_text(json.dumps(summary, indent=2) + "\n")
        self.log.info("Summary written to %s", path)
        if self._db is not None:
            try:
                import db as _db
                _db.ingest_cli_summary(self._db, summary_path=path)
                self.log.info("Run history ingested into %s", self.args.db_path)
            except Exception as exc:  # pragma: no cover - defensive
                self.log.warning("DB ingest failed (%s) — summary file is still on disk", exc)

    # ------------------------------------------------------------------
    # Confidence scoring
    #
    # Estimates how likely a live update run is to succeed without
    # issues.  Starts at 100 and subtracts for known risk factors.
    #   90-100  HIGH    — safe to auto-update
    #   70-89   MEDIUM  — likely fine, monitor closely
    #   50-69   LOW     — consider manual update
    #   <50     RISKY   — strong recommendation for manual update
    # ------------------------------------------------------------------

    def _compute_confidence(self, r: SiteReport) -> dict[str, Any]:
        rules = CONFIDENCE_RULES
        score = 100
        factors: list[str] = []
        b = r.baseline

        # WooCommerce = higher stakes (payment, orders)
        if r.has_woocommerce:
            score -= rules["woocommerce_penalty"]
            factors.append(
                f"-{rules['woocommerce_penalty']:<2}  WooCommerce site (payment/order risk)"
            )

        # Many plugin updates = more things that can break
        n_plugins = len(b.get("plugin_updates", []))
        if n_plugins > rules["plugin_updates_high_threshold"]:
            score -= rules["plugin_updates_high_penalty"]
            factors.append(
                f"-{rules['plugin_updates_high_penalty']:<2}  {n_plugins} plugin updates "
                f"(>{rules['plugin_updates_high_threshold']})"
            )
        elif n_plugins > rules["plugin_updates_med_threshold"]:
            score -= rules["plugin_updates_med_penalty"]
            factors.append(
                f"-{rules['plugin_updates_med_penalty']:<2}  {n_plugins} plugin updates "
                f"(>{rules['plugin_updates_med_threshold']})"
            )
        elif n_plugins > 0:
            score -= rules["plugin_updates_low_penalty"]
            factors.append(
                f" -{rules['plugin_updates_low_penalty']}  {n_plugins} plugin update(s)"
            )

        # Theme updates
        n_themes = len(b.get("theme_updates", []))
        if n_themes > 0:
            score -= rules["theme_updates_penalty"]
            factors.append(
                f" -{rules['theme_updates_penalty']}  {n_themes} theme update(s)"
            )

        # Core update pending
        if b.get("core_updates"):
            score -= rules["core_update_penalty"]
            factors.append(
                f" -{rules['core_update_penalty']}  WordPress core update pending"
            )

        # Large site (backup takes longer, more to go wrong)
        disk = b.get("disk", {})
        site_mb = disk.get("site_mb", 0)
        if site_mb > rules["large_site_threshold_mb"]:
            score -= rules["large_site_penalty"]
            factors.append(
                f" -{rules['large_site_penalty']}  Large site ({site_mb:.0f} MB)"
            )

        # Tight disk space
        avail_mb = disk.get("available_mb", 0)
        est_backup = disk.get("estimated_backup_mb", 0)
        if (avail_mb > 0 and est_backup > 0
                and avail_mb < est_backup * rules["tight_disk_multiplier"]):
            score -= rules["tight_disk_penalty"]
            factors.append(
                f"-{rules['tight_disk_penalty']:<2}  Tight disk space "
                f"({avail_mb:.0f} MB avail, need {est_backup:.0f} MB)"
            )

        # PHP version (older = riskier with new plugin versions)
        php = b.get("php_version", "")
        if php:
            try:
                major_minor = float(php.rsplit(".", 1)[0])
                if major_minor < rules["old_php_threshold"]:
                    score -= rules["old_php_penalty"]
                    factors.append(
                        f"-{rules['old_php_penalty']:<2}  Outdated PHP {php} "
                        f"(<{rules['old_php_threshold']})"
                    )
            except ValueError:
                pass

        # No backup plugin = we're the only safety net
        if not b.get("backup_plugins"):
            score -= rules["no_backup_plugin_penalty"]
            factors.append(
                f" -{rules['no_backup_plugin_penalty']}  No backup plugin installed"
            )

        # Staging site = lower stakes
        if r.is_staging:
            score += rules["staging_bonus"]
            factors.append(
                f"+{rules['staging_bonus']:<2}  Staging site (lower risk)"
            )

        # Nothing to update = nothing to break
        if n_plugins == 0 and n_themes == 0 and not b.get("core_updates"):
            score = 100
            factors = ["     No updates pending — nothing to change"]

        score = max(0, min(100, score))

        if score >= rules["grade_high_min"]:
            grade = "HIGH"
        elif score >= rules["grade_medium_min"]:
            grade = "MEDIUM"
        elif score >= rules["grade_low_min"]:
            grade = "LOW"
        else:
            grade = "RISKY"

        return {"score": score, "grade": grade, "factors": factors}

    # ------------------------------------------------------------------
    # Per-site report (printed after each site in both modes)
    # ------------------------------------------------------------------

    def _print_site_report(self, r: SiteReport) -> None:
        """Print a detailed per-site status block to stdout."""
        b = r.baseline
        disk = b.get("disk", {})
        conf = b.get("confidence", {})
        L = self.log.info  # shorthand

        L("")
        L("  ┌─ %s — %s", r.client, r.domain)
        L("  │")
        L("  │  WordPress:    %s", b.get("wp_version", "?"))
        L("  │  PHP:          %s", b.get("php_version", "?"))
        L("  │  Site URL:     %s", b.get("siteurl", "?"))
        L("  │  WooCommerce:  %s", "YES" if r.has_woocommerce else "no")
        L("  │  Staging:      %s", "YES" if r.is_staging else "no")
        L("  │")

        # Core updates
        core = b.get("core_updates", [])
        if core:
            target = core[0].get("version", "?") if core else "?"
            L("  │  Core update:  %s → %s", b.get("wp_version", "?"), target)
        else:
            L("  │  Core update:  up to date")

        # Themes
        theme_updates = b.get("theme_updates", [])
        all_themes = b.get("themes", [])
        L("  │")
        L("  │  Themes:       %d installed, %d need updates",
          len(all_themes), len(theme_updates))
        if theme_updates:
            for t in theme_updates:
                L("  │    %-35s  %s → %s",
                  t.get("name", "?"),
                  t.get("version", "?"),
                  t.get("update_version", "?"))

        # Plugins
        plugin_updates = b.get("plugin_updates", [])
        all_plugins = b.get("plugins", [])
        L("  │")
        L("  │  Plugins:      %d installed, %d need updates",
          len(all_plugins), len(plugin_updates))
        if plugin_updates:
            for p in plugin_updates:
                L("  │    %-35s  %s → %s",
                  p.get("name", "?"),
                  p.get("version", "?"),
                  p.get("update_version", "?"))

        # Disk
        L("  │")
        if disk:
            L("  │  Disk:         %s MB site, %s MB available, ~%s MB backup",
              f"{disk.get('site_mb', 0):.0f}",
              f"{disk.get('available_mb', 0):.0f}",
              f"{disk.get('estimated_backup_mb', 0):.0f}")
        else:
            L("  │  Disk:         not checked")

        # Backup plugins
        backup_plugins = b.get("backup_plugins", [])
        L("  │")
        if backup_plugins:
            names = ", ".join(
                f"{bp['label']} ({bp['status']}, v{bp['version']})"
                for bp in backup_plugins
            )
            L("  │  Backup tools: %s", names)
        else:
            L("  │  Backup tools: none detected")

        # Confidence
        L("  │")
        if conf:
            bar_len = conf["score"] // 5  # 0-20 chars
            bar = "█" * bar_len + "░" * (20 - bar_len)
            L("  │  Confidence:   %s %d/100 [%s]", bar, conf["score"], conf["grade"])
            for f in conf.get("factors", []):
                L("  │                %s", f)
        L("  │")
        L("  └─ %s", r.overall.upper())
        L("")

    def _print_site_execution_report(self, r: SiteReport) -> None:
        """Print a short summary of what was done on this site after execution."""
        L = self.log.info

        L("")
        L("  ┌─ %s — %s  [%s]", r.client, r.domain, r.overall.upper())
        L("  │")

        # Summarise each step
        for s in r.steps:
            icon = {"success": "✓", "failed": "✗", "skipped": "–", "planned": "◇"}.get(s.status, "?")
            L("  │  %s  %-30s  %s", icon, s.name, s.detail[:80])

        if r.failure_step:
            L("  │")
            L("  │  FAILURE:  step=%s", r.failure_step)
            # Truncate long error details for the report
            detail = r.failure_detail
            if len(detail) > 200:
                detail = detail[:200] + "..."
            L("  │            %s", detail)

        if r.rollback_result:
            L("  │  ROLLBACK: %s", r.rollback_result)

        L("  └─")
        L("")

    # ------------------------------------------------------------------
    # Final report
    # ------------------------------------------------------------------

    def _print_final_report(self) -> None:
        self.log.info("=" * 70)
        self.log.info("FINAL REPORT")
        self.log.info("=" * 70)

        if self._run_abort_reason:
            self.log.error("  Run stopped early: %s", self._run_abort_reason)
            self.log.info("")

        # In execute mode, print per-site execution summaries first
        if self.args.execute:
            for r in self.reports:
                if r.overall not in ("skipped", "pending"):
                    self._print_site_execution_report(r)
            self.log.info("-" * 70)

        # Counts
        counts = {}
        for r in self.reports:
            counts[r.overall] = counts.get(r.overall, 0) + 1
        self.log.info("  Totals:  %s",
                       "  ".join(f"{v} {k}" for k, v in sorted(counts.items())))
        self.log.info("")

        # Per-site one-liner table
        for r in self.reports:
            icon = {
                "success": "✓",
                "dry-run": "◇",
                "skipped": "–",
                "rolled-back": "↺",
                "failed": "✗",
                "pending": "?",
            }.get(r.overall, "?")

            extra = ""
            if r.overall == "skipped" and r.has_woocommerce:
                extra = "  [WooCommerce — manual review]"
            elif r.overall in ("failed", "rolled-back"):
                extra = f"  [failed at: {r.failure_step}]"
            elif r.overall == "dry-run":
                conf = r.baseline.get("confidence", {})
                if conf:
                    extra = f"  [{conf['grade']} {conf['score']}/100]"

            self.log.info(
                "  %s  %-11s  %-25s  %s%s",
                icon, r.overall.upper(), r.client, r.domain, extra,
            )

        self.log.info("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Safely update WordPress core, themes, and plugins across "
            "Cloudways client sites.  Dry-run by default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--execute", action="store_true",
        help="Perform live updates. Without this flag, the script only "
             "collects baselines and plans backups.",
    )
    p.add_argument(
        "--env-file", type=Path, default=DEFAULT_ENV,
        help=f"Path to .env file (default: {DEFAULT_ENV})",
    )
    p.add_argument(
        "--clients-dir", type=Path, default=DEFAULT_CLIENTS,
        help=f"Directory with *_cloudways.json files (default: {DEFAULT_CLIENTS})",
    )
    p.add_argument(
        "--client-file", type=Path, action="append", default=None,
        help=(
            "Process specific client JSON file(s) instead of all. "
            "Repeatable: pass --client-file once per file to process a subset."
        ),
    )
    p.add_argument(
        "--log-dir", type=Path, default=DEFAULT_LOGS,
        help=f"Directory for logs and summaries (default: {DEFAULT_LOGS})",
    )
    p.add_argument(
        "--include-woocommerce", action="store_true",
        help="Include WooCommerce sites (normally skipped for manual review).",
    )
    p.add_argument(
        "--skip-staging", action="store_true",
        help="Skip sites with is_staging=true.",
    )
    p.add_argument(
        "--skip-ssl-verify", action="store_true",
        help="Disable SSL certificate verification for HTTP health checks.",
    )
    p.add_argument(
        "--ssh-config", type=Path, default=DEFAULT_SSH_CONFIG,
        help=(
            "SSH config file to use for outbound maintenance connections "
            f"(default: {DEFAULT_SSH_CONFIG}; use /etc/ssh/ssh_config to opt "
            "back into the system config)."
        ),
    )
    p.add_argument(
        "--ssh-key", type=Path, default=None,
        help="Override SSH_KEY from .env for this run.",
    )
    p.add_argument(
        "--connect-timeout", type=int, default=20,
        help="SSH connection timeout in seconds (default: 20).",
    )
    p.add_argument(
        "--remote-timeout", type=int, default=600,
        help="Per-command remote execution timeout in seconds (default: 600).",
    )
    p.add_argument(
        "--http-timeout", type=int, default=20,
        help="HTTP health check timeout in seconds (default: 20).",
    )
    p.add_argument(
        "--skip-recent", type=int, default=24, metavar="HOURS",
        help="Skip sites that succeeded in an execute-mode run within the "
             "last N hours (default: 24; 0 = disabled). Queries the SQLite "
             "DB at --db-path, falling back to logs/wp-update-summary-*.json "
             "if the DB is empty. Makes daily reruns idempotent.",
    )
    p.add_argument(
        "--db-path", type=Path, default=DEFAULT_DB,
        help=f"SQLite DB for run history + dedupe (default: {DEFAULT_DB}). "
             "Used by --skip-recent and to ingest this run's summary at end.",
    )
    p.add_argument(
        "--no-db", action="store_true",
        help="Disable DB integration entirely (no dedupe via DB, no ingest "
             "at end of run). --skip-recent falls back to log scanning.",
    )
    p.add_argument(
        "--max-consecutive-failures", type=int, default=3,
        help="Abort an execute-mode batch after this many consecutive "
             "failed/rolled-back sites (default: 3, use 0 to disable).",
    )
    p.add_argument(
        "--stream", action="store_true",
        help="Stream all activity to stdout (tail -f style). Shows SSH "
             "commands, remote output, and all debug-level detail in real time.",
    )
    return p.parse_args()


def main() -> int:
    args = build_cli()
    updater = WPUpdater(args)
    return updater.run()


if __name__ == "__main__":
    raise SystemExit(main())
