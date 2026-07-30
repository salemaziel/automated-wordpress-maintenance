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
from collections.abc import Sequence
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

# Siteground sites live under /home/<ssh_user>/www/<domain>/public_html.
# The home dir is symlinked to /home/customer on the actual host but the
# path through the symlink is the canonical inventory form.
VALID_PATH_SG = re.compile(r"^/home/[A-Za-z0-9_-]+/www/[A-Za-z0-9._-]+/public_html$")

# Siteground SSH always uses port 18765 — non-standard, must be passed
# explicitly to ssh(1).
SITEGROUND_SSH_PORT = 18765

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

# Page-cache plugins — slug → (label, wp-cli flush subcommand).
# When detected at baseline, the plugin update loop runs the mapped flush
# command after each plugin update so verify sees fresh content (not a
# stale cached error page from before the fix). When no entry matches, the
# loop falls back to "wp cache flush" (WP core object cache, always
# available even without a cache plugin).
KNOWN_CACHE_PLUGINS = {
    "w3-total-cache":      ("W3 Total Cache",       "w3-total-cache flush all"),
    "wp-super-cache":      ("WP Super Cache",       "super-cache flush"),
    "litespeed-cache":     ("LiteSpeed Cache",      "litespeed-purge all"),
    "wp-rocket":           ("WP Rocket",            "rocket clean --confirm"),
    "wp-fastest-cache":    ("WP Fastest Cache",     "fastest-cache clear"),
    "sg-cachepress":       ("SG Optimizer",         "sg purge"),
    "cache-enabler":       ("Cache Enabler",        "cache-enabler clear"),
}

# Transient/regenerable directories excluded from both the backup archive and
# the disk-space estimate. WordPress or the cache plugin rebuilds these on
# demand, so archiving them wastes disk and can block otherwise-healthy sites
# at the disk-check gate: jsweldingandfabrication.com failed run
# 20260730T215215Z with 3.7GB of Breeze cache inside a 4.7GB tree whose real
# content was ~950MB.
#
# Paths are relative to the WordPress root. Deliberately NOT excluded:
#   wp-content/wflogs   — Wordfence firewall rules/IP reputation; small, and
#                         losing it on rollback degrades security posture.
#   wp-content/updraft  — the client's OWN backups. Dropping those during a
#                         restore is not a call this tool makes silently.
BACKUP_EXCLUDE_DIRS = (
    "wp-content/cache",               # Breeze, W3TC, WP Super Cache, Comet, ...
    "wp-content/upgrade",             # WP core's transient unpack dir
    "wp-content/upgrade-temp-backup",  # WP 6.3+ rollback staging
    "wp-content/uploads/cache",       # some plugins nest cache under uploads
    "wp-content/et-cache",            # Divi
    "wp-content/litespeed",           # LiteSpeed Cache
)

# Backup size estimation. The old 0.6 "tar.gz ≈ 50% + SQL ≈ 10%" figure was
# only ever safe because highly-compressible cache inflated the denominator.
# Measured against two real archives on 143.244.179.152 (2026-04-24):
#   kzwbjzqspp  ~1.10GB tree → 946MB tar.gz  (86%)
#   vkkueyverz  ~1.15GB tree → 931MB tar.gz  (79%)
# Once cache is excluded the remainder is mostly uploads/ (JPEG/PNG/PDF, already
# compressed), so the ratio approaches 1.0 and 0.6 becomes an UNDER-estimate
# exactly when we start relying on it.
BACKUP_SIZE_RATIO = 0.9            # tar.gz of a cache-excluded tree
BACKUP_DB_RATIO = 0.15             # SQL dump headroom
BACKUP_HEADROOM_MULTIPLIER = 2.0   # require 2x the estimate as free space

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


def _tar_exclude_flags(excludes: Sequence[str]) -> str:
    """Render BACKUP_EXCLUDE_DIRS as `tar --exclude` flags.

    Archives are created with `tar -C <wp_root> .`, so members are named
    `./wp-content/cache/...` — the patterns must carry the same `./` prefix to
    match. GNU tar honours --exclude positionally, so callers MUST place the
    result before the `.` file operand.

    Returns "" for an empty list, which keeps the generated command byte-identical
    to the pre-exclusion behaviour under --no-backup-excludes.
    """
    return " ".join(shlex.quote(f"--exclude=./{path}") for path in excludes)


def _du_exclude_paths(wp_path: str, excludes: Sequence[str]) -> list[str]:
    """Absolute paths of the excluded dirs, for the `du -sbc` subtraction.

    The disk estimate deliberately does NOT use `du --exclude`: GNU du matches
    its patterns against the path as du prints it, while tar matches against the
    archive member name. Those semantics differ enough that a silent mismatch is
    plausible, and a mismatch here means an under-reserved disk — the exact
    failure the disk-check gate exists to prevent.

    Measuring the excluded dirs separately and subtracting makes the estimate
    equal to what tar actually writes *by construction*, from this one list.
    """
    return [f"{wp_path}/{path}" for path in excludes]


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


def _extract_wpcli_json_array(raw: str) -> list | None:
    """Locate and parse the JSON array emitted by `wp ... --format=json`.

    WP-CLI prints the JSON array on stdout, but plugins/themes can emit their
    own noise (PHP notices, or an Elementor "data updater process has been
    queued. [array (...)]" dump) before OR after it. A naive `raw.find("[")`
    + `json.loads` grabs the first bracket — often one inside that noise
    (`[info]`, `[array (`) — fails to parse, and masks a *successful* update
    as a parse error (which then gets logged as a non-fatal skip).

    Scan every '[' offset and use `raw_decode`, which stops at the end of the
    first valid JSON value, so trailing noise after the array is tolerated
    too. Return the first bracket that decodes to a list; None if none do.
    """
    decoder = json.JSONDecoder()
    start = raw.find("[")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(raw[start:])
        except json.JSONDecodeError:
            start = raw.find("[", start + 1)
            continue
        if isinstance(value, list):
            return value
        start = raw.find("[", start + 1)
    return None


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
class SiteCapabilities:
    """Plugin capabilities detected at baseline-collection time.

    Drives cache-flush behavior in the plugin update loop. Future tracks
    will use this for backup-plugin selection (UpdraftPlus CLI vs tar).
    """
    backup_plugin: str = ""       # KNOWN_BACKUP_PLUGINS label, "" if none active
    cache_plugin: str = ""        # KNOWN_CACHE_PLUGINS label, "" if none active
    cache_flush_cmd: str = ""     # wp-cli subcommand; "" → fallback "cache flush"

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_plugin": self.backup_plugin,
            "cache_plugin": self.cache_plugin,
            "cache_flush_cmd": self.cache_flush_cmd,
        }


@dataclass
class SiteReport:
    """Aggregate result for one WordPress application."""
    client: str
    domain: str
    server_ip: str
    wp_path: str
    is_staging: bool
    has_woocommerce: bool
    # Hosting provider — "cloudways" (default for back-compat) or "siteground".
    # Drives path validation, SSH preflight tier, backup dest, and ownership steps.
    provider: str = "cloudways"
    # SSH port. Cloudways uses 22; Siteground uses 18765.
    ssh_port: int = 22
    overall: str = "pending"       # pending | dry-run | success | failed | rolled-back | skipped
    # Tri-state pending-updates flag set by baseline collection:
    #   None  — not yet computed
    #   True  — at least one core/theme/plugin update is available (and not in skip_items)
    #   False — site is fully up to date (or only skipped slugs have updates)
    needs_update: bool | None = None
    backup_dir: str = ""
    # True only after the pre-flight backup script has fully succeeded
    # (tar written, integrity-verified). Rollback is gated on THIS, not on
    # backup_dir — which is only a path and is set before the backup runs.
    # A failed backup must never arm a destructive rollback: at backup time
    # no mutations have happened yet, so restoring from a known-bad archive
    # could only destroy a healthy site.
    backup_ok: bool = False
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

    # Plugin capabilities detected during baseline collection. Drives
    # cache-flush behavior in the plugin update loop.
    capabilities: SiteCapabilities | None = None

    # Pre-mutation HTTP status from a single GET against the siteurl,
    # captured at the end of _step_collect_baseline. When the baseline is a
    # persistent 5xx (e.g. SeedProd "Coming Soon" plugin serves 503 by
    # design), _verify treats post-update responses with the same status as
    # healthy — without this, every plugin update on such a site would
    # false-positive as "broke the site" and roll back. None means we
    # never captured one (connection failure, or pre-fix run).
    baseline_http_status: int | None = None

    # These are used at runtime but NEVER serialised (see to_dict)
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_key_path: str = ""
    master_user: str = ""
    master_password: str = ""
    # Remote $HOME, resolved once at preflight. Siteground writes backups under
    # this path; using a resolved absolute string lets shlex.quote() be safe
    # without suppressing the variable expansion the code used to rely on.
    home_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to dict, stripping credentials."""
        return {
            "client": self.client,
            "domain": self.domain,
            "server_ip": self.server_ip,
            "wp_path": self.wp_path,
            "is_staging": self.is_staging,
            "has_woocommerce": self.has_woocommerce,
            "provider": self.provider,
            "ssh_port": self.ssh_port,
            "overall": self.overall,
            "needs_update": self.needs_update,
            "auth_method": self.auth_method,
            "auth_user": self.auth_user,
            "original_owner": self.original_owner,
            "backup_dir": self.backup_dir,
            "backup_ok": self.backup_ok,
            "failure_step": self.failure_step,
            "failure_detail": self.failure_detail,
            "rollback_result": self.rollback_result,
            "baseline": self.baseline,
            "skip_items": self.skip_items,
            "capabilities": self.capabilities.to_dict() if self.capabilities else None,
            "baseline_http_status": self.baseline_http_status,
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


def _is_already_deactivated(exc: BaseException) -> bool:
    """True if the wp-cli error means maintenance-mode was already off.

    The X/Pro theme updater self-disables maintenance mode after its own
    update, so our later `wp maintenance-mode deactivate` call is a no-op
    that exits 1 with `Error: Maintenance mode already deactivated.`. This
    is the discriminator that lets us demote that benign case to DEBUG.
    """
    return "already deactivated" in str(exc).lower()


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


def baseline_pending_updates(
    baseline: dict[str, Any],
    skip_items: list[dict[str, Any]] | None = None,
) -> dict[str, list[Any]]:
    """Return the subset of baseline updates that would actually run.

    Filters out plugins/themes whose slug is configured as a skip in
    notes.json — those will never be touched even if WP-CLI reports an
    update available, so they shouldn't count toward "this site needs work".

    Returns a dict with keys 'core', 'plugins', 'themes'. Empty lists mean
    nothing pending in that category. A site is fully up to date when all
    three lists are empty.
    """
    skip_plugin_slugs = {
        (it.get("slug") or "").strip()
        for it in (skip_items or [])
        if (it.get("type") or "").strip().lower() == "plugin"
    }
    skip_theme_slugs = {
        (it.get("slug") or "").strip()
        for it in (skip_items or [])
        if (it.get("type") or "").strip().lower() == "theme"
    }
    return {
        "core": list(baseline.get("core_updates") or []),
        "plugins": [
            p for p in (baseline.get("plugin_updates") or [])
            if (p.get("name") or "").strip() not in skip_plugin_slugs
        ],
        "themes": [
            t for t in (baseline.get("theme_updates") or [])
            if (t.get("name") or "").strip() not in skip_theme_slugs
        ],
    }


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


def is_siteground_doc(doc: dict[str, Any]) -> bool:
    """Detect a Siteground per-domain inventory doc by its schema marker."""
    schema = (doc.get("schema") or "").strip().lower()
    if schema.startswith("siteground_"):
        return True
    return (doc.get("provider") or "").strip().lower() == "siteground"


def siteground_to_internal_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert a Siteground v1 inventory doc into the internal Cloudways-
    shaped dict that _process_client_file consumes.

    No values are invented — SSH host, user, port, key env, and WordPress
    path are read from the SG doc and placed into the Cloudways field shape
    with provider markers attached. The result has exactly one application.
    """
    domain = (doc.get("domain") or "").strip()
    ssh = doc.get("ssh") or {}
    wp = doc.get("wordpress") or {}
    key_env = (ssh.get("key_env") or "SSH_KEY_SG").strip()
    return {
        "_provider": "siteground",
        "client_name": domain or "siteground-site",
        # Cloudways stores an IP here; on Siteground it's an SSH hostname.
        # ssh(1) accepts either form, so we just store it verbatim.
        "server_ip_address": (ssh.get("host") or "").strip(),
        "master_credentials": {},  # SG has no master concept
        "applications": [{
            "website_domain": domain,
            "path_to_public_html": (wp.get("path") or "").strip(),
            "sftp_credentials": {
                "username": (ssh.get("user") or "").strip(),
                "ssh_key": f"${key_env}",
                "password": "",  # SG is key-only
            },
            "environment_flags": {
                "wp_cli_installed": True,
                "is_staging": bool(doc.get("is_staging", False)),
                "has_woocommerce": bool(doc.get("has_woocommerce", False)),
            },
            "_provider": "siteground",
            "_ssh_port": int(ssh.get("port") or SITEGROUND_SSH_PORT),
            "_enabled": bool(doc.get("enabled", True)),
            "_skip_reason": doc.get("skip_reason"),
        }],
    }


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

        # Summary-driven "no updates available" skip state.
        # Maps domain -> {"pending_plugin_slugs", "pending_theme_slugs",
        # "core_count"} captured from the most recent eligible dry-run.
        # _process_client_file re-applies the CURRENT notes.json skip_items
        # to those cached lists before honoring the skip, so a notes.json
        # edit between dry-run and execute can't cause us to skip a site
        # whose previously-skipped plugin update is now actionable.
        # Populated only in execute mode, and only when neither --recheck-updates
        # nor --no-skip-up-to-date is in effect. Inline-recheck handles its own
        # skip path inside _process_site.
        self._no_update_domains: dict[str, dict[str, Any]] = \
            self._load_no_update_domains()
        if self._no_update_domains:
            self.log.info(
                "Up-to-date skip active: %d domain(s) had needs_update=false "
                "in a dry-run within the last %dm — will be skipped if "
                "current notes.json skip rules still cover the cached pending set",
                len(self._no_update_domains), self.args.skip_up_to_date_ttl,
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
        self._maybe_update_sheet()
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
        # (clients/cloudways/<base>/<slug>_cloudways.json or
        #  clients/siteground/<domain>/<domain>_siteground.json).
        base = self.args.clients_dir
        provider = (getattr(self.args, "provider", "auto") or "auto").lower()
        suffixes: list[str] = []
        if provider in ("auto", "cloudways"):
            suffixes.append("_cloudways.json")
        if provider in ("auto", "siteground"):
            suffixes.append("_siteground.json")
        found: set[Path] = set()
        for suffix in suffixes:
            for pattern in (
                f"*{suffix}",
                f"*/*{suffix}",
                f"*/*/*{suffix}",
            ):
                for path in base.glob(pattern):
                    # Files moved to clients/_archived/ are excluded from
                    # fleet runs; restore via the webui (or by moving the
                    # file out of _archived/) to put them back in scope.
                    if "_archived" in path.parts:
                        continue
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
                    # Normalize so callers comparing report.domain match
                    # rows written from differently-formatted inventory.
                    return {_normalize_domain(d) for d in domains if d}
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
            if not isinstance(data, dict):
                continue
            if data.get("mode") != "execute":
                continue
            sites = data.get("sites")
            if not isinstance(sites, list):
                continue
            for entry in sites:
                if not isinstance(entry, dict):
                    continue
                if entry.get("overall") == "success" and entry.get("domain"):
                    domains.add(_normalize_domain(str(entry["domain"])))
        return {d for d in domains if d}

    def _load_no_update_domains(self) -> dict[str, dict[str, Any]]:
        """Return a mapping of domains whose most recent dry-run summary
        (within the TTL) reported needs_update=false, along with the
        pre-skip pending slug lists from that summary.

        Each value: {"pending_plugin_slugs", "pending_theme_slugs",
        "core_count"}. The caller (_process_client_file) re-applies the
        CURRENT notes.json skip_items to those cached lists; only
        domains where current skips still cover every cached pending
        slug are eligible for the skip. This guards against an operator
        removing a notes.json skip between dry-run and execute.

        Only active in execute mode. Disabled when --recheck-updates is set
        (the caller wants a fresh inline check) or --no-skip-up-to-date is
        set (operator override).

        Multiple summaries may mention the same domain; the most recent
        one wins. If the most-recent record has needs_update=true (or is
        missing the flag — older summary file format), the domain is NOT
        added to the returned mapping.
        """
        if not self.args.execute:
            return {}
        if getattr(self.args, "no_skip_up_to_date", False):
            return {}
        if getattr(self.args, "recheck_updates", False):
            return {}
        minutes = getattr(self.args, "skip_up_to_date_ttl", 0) or 0
        if minutes <= 0:
            return {}

        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        # Per-domain latest record across BOTH dry-run and execute summaries.
        # Execute entries are tracked only to *invalidate* an older dry-run
        # for the same domain: if anything happened between the cached
        # needs_update=false dry-run and now, we won't trust the cache.
        # Result filter below keeps only domains whose most-recent record
        # is a dry-run with needs_update=false AND a valid baseline.
        latest_per_domain: dict[
            str, tuple[datetime, str, bool | None, dict[str, Any] | None]
        ] = {}
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
            if not isinstance(data, dict):
                continue
            mode = data.get("mode")
            if mode not in ("dry-run", "execute"):
                continue
            sites = data.get("sites")
            if not isinstance(sites, list):
                continue
            for entry in sites:
                if not isinstance(entry, dict):
                    continue
                raw_domain = entry.get("domain")
                if not raw_domain:
                    continue
                domain = _normalize_domain(str(raw_domain))
                if not domain:
                    continue
                # Dry-run entries only count if baseline was actually
                # collected — a site that errored out pre-baseline has no
                # signal. Execute entries count regardless of outcome;
                # they're only noted to supersede older dry-runs.
                if mode == "dry-run" and entry.get("overall") != "dry-run":
                    continue
                needs = entry.get("needs_update")
                baseline = entry.get("baseline")
                if not isinstance(baseline, dict):
                    baseline = None
                existing = latest_per_domain.get(domain)
                if existing is None or existing[0] < run_dt:
                    latest_per_domain[domain] = (run_dt, mode, needs, baseline)

        result: dict[str, dict[str, Any]] = {}
        for domain, (_, mode, needs, baseline) in latest_per_domain.items():
            # Only dry-run summaries qualify as cache hits. A newer execute
            # summary (success, failed, or rolled-back) for the same domain
            # invalidates any older dry-run within the TTL — see codex G4
            # (mixed dry-run/execute history stale-skip hole).
            if mode != "dry-run":
                continue
            if needs is not False:
                continue
            # Require a structurally-valid baseline. A dry-run that claims
            # needs_update=false but has no/invalid baseline lists cannot
            # validate skip-drift, so refuse the cache hit rather than
            # degrade silently to empty pending lists (codex G2).
            if baseline is None:
                continue
            plugins = baseline.get("plugin_updates")
            themes = baseline.get("theme_updates")
            core = baseline.get("core_updates")
            if not (
                isinstance(plugins, list)
                and isinstance(themes, list)
                and isinstance(core, list)
            ):
                continue
            result[domain] = {
                "pending_plugin_slugs": [
                    (p.get("name") or "").strip()
                    for p in plugins
                    if isinstance(p, dict)
                ],
                "pending_theme_slugs": [
                    (t.get("name") or "").strip()
                    for t in themes
                    if isinstance(t, dict)
                ],
                "core_count": len(core),
            }
        return result

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

        # Siteground per-domain inventory has a different shape (one site
        # per file, no client_name/applications array). Translate it into
        # the internal Cloudways-shaped dict so the rest of this method
        # can stay schema-agnostic.
        if is_siteground_doc(doc):
            doc = siteground_to_internal_doc(doc)

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
            # Domain matching is normalized so inventory entries that differ
            # only by scheme/www/case still hit the same cache rows that the
            # dry-run wrote.
            norm_domain = _normalize_domain(report.domain)
            if norm_domain in self._recent_successes:
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
            cached = self._no_update_domains.get(norm_domain)
            if cached is not None:
                # Re-apply *current* skip rules to the cached pre-filter
                # pending slugs. If current skips still cover every slug
                # that had an update available at dry-run time, the cached
                # decision is still valid. Otherwise — e.g. an operator
                # just removed a skip — fall through and run the site.
                current_skip_plugins = {
                    (it.get("slug") or "").strip()
                    for it in report.skip_items
                    if (it.get("type") or "").strip().lower() == "plugin"
                }
                current_skip_themes = {
                    (it.get("slug") or "").strip()
                    for it in report.skip_items
                    if (it.get("type") or "").strip().lower() == "theme"
                }
                still_pending_plugins = [
                    s for s in cached["pending_plugin_slugs"]
                    if s and s not in current_skip_plugins
                ]
                still_pending_themes = [
                    s for s in cached["pending_theme_slugs"]
                    if s and s not in current_skip_themes
                ]
                stale = bool(
                    still_pending_plugins
                    or still_pending_themes
                    or cached["core_count"]
                )
                if not stale:
                    report.overall = "skipped"
                    report.needs_update = False
                    report.failure_detail = (
                        "no updates available in latest dry-run "
                        f"(within {self.args.skip_up_to_date_ttl}m TTL)"
                    )
                    self._record_step(
                        report, "up-to-date-skip", "skipped",
                        "latest dry-run reported needs_update=false; "
                        "use --recheck-updates or --no-skip-up-to-date to override",
                    )
                    self.log.info(
                        "SKIP  %s — up to date per recent dry-run", report.domain,
                    )
                    self.reports.append(report)
                    continue
                # Notes/skip rules changed — log why we're NOT skipping.
                self.log.info(
                    "  up-to-date skip invalidated for %s — notes.json now "
                    "leaves %d plugin / %d theme / %d core update(s) actionable",
                    report.domain, len(still_pending_plugins),
                    len(still_pending_themes), cached["core_count"],
                )
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

        # Provider marker is set on the doc by siteground_to_internal_doc()
        # at load time, and propagated to the app entry. Cloudways files
        # don't carry the marker; default to cloudways.
        provider = (
            (app.get("_provider") or doc.get("_provider") or "cloudways").lower()
        )

        # Disabled-in-inventory: Siteground inventory carries enabled=false
        # for sites with DNS pointing elsewhere (edtec.com,
        # foundationforseniorcare.org). Skip them with a clear reason.
        if app.get("_enabled") is False:
            reason = app.get("_skip_reason") or "disabled in inventory"
            raise InventoryError(f"disabled: {reason}")

        if not domain or domain.startswith("["):
            raise InventoryError("missing website_domain")

        path_re = VALID_PATH_SG if provider == "siteground" else VALID_PATH
        if not path_re.match(wp_path):
            raise InventoryError(
                f"invalid path_to_public_html for provider={provider}: {wp_path!r}"
            )
        if not isinstance(sftp, dict):
            raise InventoryError("sftp_credentials is not an object")
        if not isinstance(flags, dict):
            raise InventoryError("environment_flags is not an object")

        master_creds = doc.get("master_credentials", {}) or {}
        ssh_port = (
            int(app.get("_ssh_port") or SITEGROUND_SSH_PORT)
            if provider == "siteground"
            else 22
        )

        return SiteReport(
            client=doc["client_name"],
            domain=domain,
            server_ip=doc["server_ip_address"],
            wp_path=wp_path,
            is_staging=bool(flags.get("is_staging", False)),
            has_woocommerce=bool(flags.get("has_woocommerce", False)),
            provider=provider,
            ssh_port=ssh_port,
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

            # Default execute-mode behavior: once the inline baseline says
            # nothing is pending for this site, bail out before the backup,
            # update, and verify steps. The operator can disable this with
            # --no-skip-up-to-date to force every site through the full
            # mutation+verify path (e.g. for end-to-end smoke testing).
            # (--recheck-updates still has effect upstream — it disables
            # the summary-cache short-circuit so we always reach this
            # point inline rather than skipping baseline collection.)
            if (self.args.execute
                    and r.needs_update is False
                    and not getattr(self.args, "no_skip_up_to_date", False)):
                self._record_step(
                    r, "up-to-date-skip", "skipped",
                    "inline baseline found no pending updates "
                    "(use --no-skip-up-to-date to force a full run)",
                )
                r.overall = "skipped"
                self.log.info(
                    "SKIP  %s — inline baseline: no updates available",
                    r.domain,
                )
                return

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
                        # already toggled it off is the common benign case
                        # (X/Pro theme self-disables maintenance mode after
                        # its own update, so our later deactivate is a
                        # no-op). Suppress that specific case to DEBUG;
                        # surface anything else as a real warning so a PHP
                        # fatal preventing wp-cli from booting isn't
                        # invisible — final-verification will still catch
                        # it either way.
                        if _is_already_deactivated(exc):
                            self.log.debug(
                                "Maintenance mode was already off (wp-cli "
                                "self-disabled during an update)  |  %s",
                                r.domain,
                            )
                        else:
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
            # Baseline set needs_update from the *pre-update* state. After a
            # successful execute that state is stale — everything pending was
            # applied — so flip it to reflect post-update reality. Failed and
            # rolled-back paths intentionally keep the original True/False so
            # downstream readers can see what was outstanding at the time of
            # the failure.
            r.needs_update = False
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

            if self.args.execute and r.backup_ok:
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
            if self.args.execute and r.backup_ok:
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

        On Cloudways, runs a three-tier auth cascade:
          1. SSH key + each candidate app-scoped user (wpupdates,
             wpupdates-stage, wpupdates-2, ...) until one succeeds.
          2. SSH key + master username — same key, but the master user has
             server-wide access to all application directories.
          3. sshpass + master password — last resort when the key isn't
             authorized for the master user.

        On Siteground, the cascade is skipped — SG has no master concept;
        each domain is its own user owning its own files, key auth only.

        When master fallback is used on Cloudways (tier 2 or 3), r.auth_method
        is set to "master" so downstream steps know to capture and restore
        file ownership after mutations. r.auth_user is always populated with
        the username that actually authenticated.
        """
        t0 = ts()
        r.auth_method = "key"

        # Siteground: single-tier key auth with the inventory's SSH user.
        # No master fallback — falling through to tier 2/3 would just
        # produce confusing "no master_user" errors.
        if r.provider == "siteground":
            self._ssh(r, "echo 'ssh-ok'")
            self._wp(r, "core is-installed")
            # Resolve $HOME once so backup paths can be absolute. Without this,
            # shlex.quote() on a literal '$HOME/...' suppresses expansion and
            # the remote shell creates a directory literally named "$HOME"
            # inside the WP path — which is the webroot.
            r.home_dir = self._ssh(r, 'printf %s "$HOME"').strip()
            if not r.home_dir.startswith("/"):
                raise SSHError(
                    f"failed to resolve $HOME on {r.ssh_user}@{r.server_ip}: "
                    f"got {r.home_dir!r}"
                )
            r.auth_user = r.ssh_user
            self._record_step(
                r, "ssh-preflight", "success",
                f"SSH reachable at {r.server_ip}:{r.ssh_port} as {r.ssh_user} "
                f"(siteground, auth=key, home={r.home_dir})", t0,
            )
            return

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

        # Detect active plugin capabilities. Only active plugins drive
        # behavior — an installed-but-deactivated cache plugin doesn't
        # serve cached pages. First match wins per category; we don't
        # try to compose multiple cache plugins (a misconfiguration we
        # log but don't try to repair).
        caps = SiteCapabilities()
        for p in plugins:
            if (p.get("status", "") or "").lower() != "active":
                continue
            slug = p.get("name", "")
            if not caps.backup_plugin and slug in KNOWN_BACKUP_PLUGINS:
                caps.backup_plugin = KNOWN_BACKUP_PLUGINS[slug]
            if not caps.cache_plugin and slug in KNOWN_CACHE_PLUGINS:
                label, flush_cmd = KNOWN_CACHE_PLUGINS[slug]
                caps.cache_plugin = label
                caps.cache_flush_cmd = flush_cmd
        r.capabilities = caps
        if caps.cache_plugin:
            self.log.info("  Cache plugin detected: %s  |  %s",
                          caps.cache_plugin, r.domain)

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

        pending = baseline_pending_updates(r.baseline, r.skip_items)
        r.needs_update = bool(pending["core"] or pending["plugins"] or pending["themes"])
        r.baseline["pending_after_skips"] = {
            "core": len(pending["core"]),
            "plugin_slugs": [p.get("name", "") for p in pending["plugins"]],
            "theme_slugs": [t.get("name", "") for t in pending["themes"]],
        }

        # Capture the live HTTP status BEFORE any mutation. SeedProd
        # "Coming Soon", UnderConstructionPage, WPMaintenance, etc. all
        # serve a styled splash page with HTTP 503 + Retry-After by design.
        # Without this snapshot, the post-update HTTP verifier would
        # interpret that 503 as evidence we broke the site and trigger an
        # unnecessary rollback (which then also "fails" verification for
        # the same reason — the splash is still 503).
        r.baseline_http_status = self._capture_http_status(r.domain)
        if r.baseline_http_status and r.baseline_http_status >= 500:
            self.log.info(
                "Baseline HTTP %d for %s — treating as the healthy "
                "state for post-mutation verify checks (likely an "
                "intentional under-construction / archive splash plugin).",
                r.baseline_http_status, r.domain,
            )

        self._record_step(
            r, "baseline", "success",
            f"WP {r.baseline['wp_version']}  |  "
            f"{len(r.baseline['plugin_updates'])} plugin updates  |  "
            f"{len(r.baseline['theme_updates'])} theme updates"
            + ("  |  needs_update=true" if r.needs_update else "  |  up to date"),
            t0,
        )

    # ------------------------------------------------------------------
    # Step: Pre-flight backup
    #
    # Backups go to <app_dir>/private_html/wp-maintenance-backups/<run_id>/
    # private_html is group-writable by www-data (same group as the app SSH
    # user), not web-accessible, and persistent across reboots.
    # ------------------------------------------------------------------

    def _active_excludes(self) -> Sequence[str]:
        """The exclusion list in force for this run.

        Single accessor so the disk estimate, the backup archive and the
        rollback forensic snapshot can never consult different lists.
        """
        if getattr(self.args, "no_backup_excludes", False):
            return ()
        return BACKUP_EXCLUDE_DIRS

    def _tar_excludes(self) -> str:
        """`tar --exclude` fragment, trailing space included when non-empty."""
        flags = _tar_exclude_flags(self._active_excludes())
        return f"{flags} " if flags else ""

    def _step_disk_check(self, r: SiteReport) -> None:
        """
        Check available disk space and estimate backup size BEFORE writing
        anything.  A WordPress backup needs room for:
          - A compressed tar of public_html (minus BACKUP_EXCLUDE_DIRS)
          - A full SQL dump of the database

        The estimate is sized against what tar will ACTUALLY write, not the
        whole tree: transient cache dirs are excluded from both, measured from
        the same BACKUP_EXCLUDE_DIRS list so the two cannot drift apart. If
        available space is less than BACKUP_HEADROOM_MULTIPLIER x the estimate
        we abort — filling a disk on a shared Cloudways server could take down
        every app on that instance.

        This check runs in both dry-run and execute mode so the operator
        always sees the disk health.
        """
        t0 = ts()

        excl_paths = _du_exclude_paths(r.wp_path, self._active_excludes())

        # du -sb  = total bytes of public_html
        # du -sbc = grand total of the excluded dirs (last line); missing paths
        #           are skipped via 2>/dev/null and simply contribute 0, so a
        #           site without a cache dir measures the same as one with an
        #           empty cache dir. No `set -e` here — du exits non-zero when
        #           any operand is absent, which is the normal case.
        # df -B1  = available bytes on the partition
        if excl_paths:
            quoted = " ".join(shlex.quote(p) for p in excl_paths)
            excl_line = (
                f"excl_bytes=$(du -sbc {quoted} 2>/dev/null"
                f" | tail -1 | awk '{{print $1}}')"
            )
        else:
            excl_line = "excl_bytes=0"

        check_script = f"""\
du_bytes=$(du -sb {shlex.quote(r.wp_path)} 2>/dev/null | awk '{{print $1}}')
{excl_line}
avail_bytes=$(df -B1 {shlex.quote(r.wp_path)} 2>/dev/null | awk 'NR==2{{print $4}}')
echo "${{du_bytes:-0}} ${{excl_bytes:-0}} ${{avail_bytes:-0}}"
"""
        raw = self._ssh(r, check_script).strip()
        parts = raw.split()
        try:
            if len(parts) == 2:
                # Legacy two-field response (older agent, or a shell that
                # dropped the middle field). Degrade to "nothing excluded"
                # rather than crashing — the estimate is then conservative.
                site_bytes, avail_bytes = (int(p) for p in parts)
                excl_bytes = 0
            else:
                site_bytes, excl_bytes, avail_bytes = (int(p) for p in parts)
        except ValueError:
            self._record_step(r, "disk-check", "success",
                              f"could not parse disk info (raw={raw!r}), proceeding", t0)
            return

        # The excluded dirs are a subset of the tree, so excl > site is
        # impossible and means the reading is untrustworthy (a du that followed
        # a symlink out of the tree, a path collision, a garbled response).
        # Do NOT clamp to site_bytes — that would make backed_up 0 and let any
        # site pass. Discard the exclusion entirely and size against the whole
        # tree, which is the conservative direction.
        if excl_bytes < 0 or excl_bytes > site_bytes:
            self.log.warning(
                "disk-check: implausible exclusion reading (%d of %d bytes) "
                "for %s — sizing backup against the full tree",
                excl_bytes, site_bytes, r.domain,
            )
            excl_bytes = 0

        site_mb = site_bytes / (1024 * 1024)
        excluded_mb = excl_bytes / (1024 * 1024)
        # What tar will actually write — the whole tree minus the transient dirs.
        backed_up_mb = site_mb - excluded_mb
        avail_mb = avail_bytes / (1024 * 1024)
        est_backup_mb = backed_up_mb * (BACKUP_SIZE_RATIO + BACKUP_DB_RATIO)
        required_mb = est_backup_mb * BACKUP_HEADROOM_MULTIPLIER

        # site_mb intentionally stays the FULL tree: the large_site_threshold_mb
        # confidence rule still wants to know a 4.7GB tree is slow to walk, and
        # keeping the key's meaning stable keeps historical summaries comparable.
        r.baseline["disk"] = {
            "site_mb": round(site_mb, 1),
            "excluded_mb": round(excluded_mb, 1),
            "backed_up_mb": round(backed_up_mb, 1),
            "available_mb": round(avail_mb, 1),
            "estimated_backup_mb": round(est_backup_mb, 1),
        }

        excl_note = (
            f" ({excluded_mb:.0f}MB transient excluded)" if excluded_mb > 0 else ""
        )

        if avail_mb < required_mb:
            detail = (
                f"INSUFFICIENT DISK — site={site_mb:.0f}MB{excl_note}, "
                f"backing up {backed_up_mb:.0f}MB, "
                f"available={avail_mb:.0f}MB, need≥{required_mb:.0f}MB"
            )
            self.log.error("⚠ %s  |  %s", detail, r.domain)
            self._record_step(r, "disk-check", "failed", detail, t0)
            raise HealthCheckError(detail)

        detail = (
            f"site={site_mb:.0f}MB{excl_note}, "
            f"backing up {backed_up_mb:.0f}MB, available={avail_mb:.0f}MB, "
            f"est_backup={est_backup_mb:.0f}MB — OK"
        )
        self._record_step(r, "disk-check", "success", detail, t0)

    def _step_backup(self, r: SiteReport) -> None:
        t0 = ts()

        # Backup destination is provider-specific:
        # - Cloudways: under the app's private_html sibling (sibling of public_html).
        # - Siteground: <home>/wp-maintenance-backups/<run_id>/, where <home> is
        #   resolved at preflight (r.home_dir). Using a literal "$HOME" here is
        #   not safe — every callsite quotes paths with shlex.quote(), which
        #   suppresses variable expansion and would create a directory named
        #   "$HOME" inside the WP path (the webroot).
        if r.provider == "siteground":
            if not r.home_dir:
                raise SSHError(
                    f"backup: r.home_dir not set for siteground site {r.domain}"
                )
            backup_dir = f"{r.home_dir}/wp-maintenance-backups/{self.run_id}"
        else:
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

        # Transient dirs are excluded from the archive. The flags must precede
        # the `.` operand — GNU tar applies --exclude positionally. The trailing
        # space is part of the rendered fragment; an empty list yields "" so the
        # command stays byte-identical to the pre-exclusion form.
        tar_excludes = self._tar_excludes()

        # The backup script is piped via stdin to avoid quoting issues.
        # It creates:
        #   preflight.sql       — full DB dump with DROP TABLE statements
        #   public_html.tar.gz  — compressed snapshot of the app, minus
        #                         BACKUP_EXCLUDE_DIRS (regenerable cache)
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
tar -czf {shlex.quote(backup_dir + '/public_html.tar.gz')} -C {shlex.quote(r.wp_path)} {tar_excludes}. 2>&1 || _tar_rc=$?
[ "$_tar_rc" -le 1 ] || exit "$_tar_rc"
# Verify both backup files are non-empty
test -s {shlex.quote(backup_dir + '/preflight.sql')}
test -s {shlex.quote(backup_dir + '/public_html.tar.gz')}
# Verify archive is readable and contains wp-content/ (guards against partial writes).
# Run in a subshell with pipefail disabled: grep -q exits on first match sending SIGPIPE
# to tar, which would otherwise cause pipefail to report a false failure.
(set +o pipefail; tar -tzf {shlex.quote(backup_dir + '/public_html.tar.gz')} 2>/dev/null | grep -qE '(^|/)wp-content/') || {{ echo 'backup-integrity-fail: wp-content/ missing from archive'; exit 1; }}
# Verify the archive also contains wp-config.php. Without it a rollback would
# restore a tree WordPress cannot boot — wp-content/ alone is not a usable site.
(set +o pipefail; tar -tzf {shlex.quote(backup_dir + '/public_html.tar.gz')} 2>/dev/null | grep -qE '(^|/)wp-config\\.php$') || {{ echo 'backup-integrity-fail: wp-config.php missing from archive'; exit 1; }}
# Fingerprint the live wp-config.php so the rollback path can later prove the
# restored copy is byte-for-byte complete (catches a truncated/partial extract
# that leaves a present-but-broken config). CWD is the WP root (cd above).
test -s wp-config.php || {{ echo 'backup-integrity-fail: wp-config.php missing or empty in live tree'; exit 1; }}
sha256sum wp-config.php | awk '{{print $1}}' > {shlex.quote(backup_dir + '/wp-config.sha256')}
wc -c < wp-config.php | tr -d ' ' > {shlex.quote(backup_dir + '/wp-config.bytes')}
echo 'backup-ok'
"""
        # If the backup script fails, _ssh raises and backup_ok stays False,
        # so the failure handler in _process_site will NOT roll back. That is
        # the correct outcome: no updates have run yet, the live site is
        # untouched, and the only archive we have is known-bad.
        self._ssh(r, script, timeout=self.args.remote_timeout)
        r.backup_ok = True
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

    def _run_theme_update_structured(self, r: SiteReport, slug: str) -> dict:
        """Run `wp theme update <slug> --format=json` and return the result dict.

        Mirrors `_run_plugin_update_structured`: a theme that cannot be
        updated (premium theme not on wordpress.org, expired license, failed
        zip fetch) often makes wp-cli emit an "Error" status — sometimes with
        a zero exit code — so we cannot trust the exit code alone. See that
        method for the meaning of the `_exit_nonzero`/`_parse_error`/`_no_entry`
        signals.
        """
        try:
            raw = self._wp(
                r, f"theme update {shlex.quote(slug)} --format=json",
                timeout=self.args.remote_timeout,
            )
        except (SSHError, WPCliError) as exc:
            return {"name": slug, "status": "Error", "_exit_nonzero": True, "_error": str(exc)}

        # Robustly locate the JSON array even when a theme emits its own
        # noise (PHP notices, etc.) before or after it.
        entries = _extract_wpcli_json_array(raw)
        if entries is None:
            return {"name": slug, "status": "Error", "_parse_error": True, "_raw": raw}

        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == slug:
                return entry
        return {"name": slug, "status": "Error", "_no_entry": True}

    # ------------------------------------------------------------------
    # Step: Update themes (sequential, one-by-one)
    #
    # Classification mirrors the plugin path: a theme that *cannot* be
    # updated (license/zip/auth error) but leaves the site healthy is
    # recorded as `skipped` and we move on — a stuck theme is never a
    # reason to roll back working updates or block the rest of the run.
    # Only a verify FAILURE after the attempt (the site is actually
    # broken) escalates to a full-site rollback.
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
            self.log.info(
                "  Updating theme  %s  (%s → %s)  |  %s",
                slug, ver_from, ver_to, r.domain,
            )

            result = self._run_theme_update_structured(r, slug)
            # Retry once on transient signals (empty output / no entry / ssh
            # blip), matching the plugin path.
            if result.get("_parse_error") and not (result.get("_raw") or "").strip():
                self.log.info("  ↻ Retrying theme update (empty output)  |  %s", slug)
                result = self._run_theme_update_structured(r, slug)
            elif result.get("_no_entry"):
                self.log.info("  ↻ Retrying theme update (no entry)  |  %s", slug)
                result = self._run_theme_update_structured(r, slug)
            elif result.get("_exit_nonzero"):
                self.log.info(
                    "  ↻ Retrying theme update (ssh/wpcli failure: %s)  |  %s",
                    result.get("_error", "?"), slug,
                )
                result = self._run_theme_update_structured(r, slug)

            status = (result.get("status", "") or "").strip().lower()
            ver_to_reported = result.get("version") or ver_to or "?"

            self._flush_cache(r, slug)

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
                # Theme update reported an error but the site is still
                # healthy — premium theme not in the repo, expired license,
                # failed zip fetch. The theme simply wasn't updated. Record
                # and continue; do NOT roll back, do NOT block remaining
                # themes/plugins.
                err_msg = _extract_plugin_error(result)
                detail = f"{slug} {ver_from}→{ver_to} non-fatal error: {err_msg}"
                self._record_step(r, step, "skipped", detail, t0)
                self.log.warning(
                    "  ⚠ Skipping theme  %s  (%s → %s): non-fatal error: %s  |  %s",
                    slug, ver_from, ver_to, err_msg, r.domain,
                )
                continue

            # verify FAILED — the update genuinely broke the site. Themes
            # have no per-item "deactivate" isolation like plugins, so this
            # escalates straight to the full-site rollback via _process_site.
            detail = f"{slug} {ver_from}→{ver_to} FAILED: {verify_exc}"
            self._record_step(r, step, "failed", detail, t0)
            raise verify_exc

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

        # Robustly locate the JSON array even when a plugin emits its own
        # noise (PHP notices, Elementor's "[array (...)]" dump) around it.
        entries = _extract_wpcli_json_array(raw)
        if entries is None:
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

            # Flush page/object cache before verify so the HTTP check sees
            # post-update content, not a stale cached error page from
            # before the fix. This is the highest-leverage win for sites
            # behind aggressive page-cache plugins (LiteSpeed, WP Rocket,
            # SG Optimizer) — those caches outlive the actual update and
            # produce false-positive verify failures.
            self._flush_cache(r, slug)

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
    #      doesn't contain wp-content/ AND wp-config.php (corrupt/wrong archive)
    #   1. Archive the failed state (for post-mortem analysis)
    #   2. Best-effort chown so wipe can recurse into restrictive-mode dirs
    #   3. Wipe public_html contents (tolerate partial; verify-empty after)
    #   4. Extract the pre-flight tar (only if wipe completed)
    #   5. Backstop: verify wp-content/ landed and wp-config.php restored
    #      byte-for-byte (matches the fingerprint captured at backup time)
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

        # Recreated after the extract (step 5c). Rendered as `:` when nothing is
        # excluded — a bare `mkdir -p` with no operands exits non-zero, which
        # under `set -euo pipefail` would abort the rollback itself.
        _mkdirs = _du_exclude_paths(r.wp_path, self._active_excludes())
        rollback_mkdir_line = (
            "mkdir -p " + " ".join(shlex.quote(p) for p in _mkdirs)
            if _mkdirs else ":"
        )

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
# 0c. Refuse to extract a backup whose archive lacks wp-config.php. Restoring a
#     config-less tree leaves WordPress unbootable; better to abort and force a
#     manual recovery than to repopulate public_html with a non-bootable site.
if ! (set +o pipefail; tar -tzf {shlex.quote(fs_backup)} 2>/dev/null | grep -qE '(^|/)wp-config\\.php$'); then
    echo "rollback-abort: {fs_backup} missing wp-config.php — refusing to extract" >&2
    exit 95
fi
# 1. Archive the broken state for forensic analysis. Same exclusions as the
#    pre-flight backup: this runs DURING a rollback, on a disk that is often
#    already under pressure, and writing gigabytes of regenerable cache here
#    is the fastest way to turn a recoverable failure into an unrecoverable one.
tar -czf {shlex.quote(failed_snapshot)} -C {shlex.quote(r.wp_path)} {self._tar_excludes()}. 2>/dev/null || true
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
# 5b. Verify wp-config.php came back intact. A present-but-truncated config is
#     worse than an obvious failure — the site half-boots and silently breaks.
#     Compare against the fingerprint captured at backup time; if it's absent
#     (older backup predating this check) fall back to a non-empty assertion so
#     we never silently trust a corrupt restore.
_cfg={shlex.quote(r.wp_path + "/wp-config.php")}
if [ ! -s "$_cfg" ]; then
    echo "rollback-abort: wp-config.php missing or empty after restore — manual recovery required" >&2
    exit 94
fi
if [ -f {shlex.quote(r.backup_dir + "/wp-config.sha256")} ]; then
    _want=$(cat {shlex.quote(r.backup_dir + "/wp-config.sha256")})
    _got=$(sha256sum "$_cfg" | awk '{{print $1}}')
    if [ "$_want" != "$_got" ]; then
        echo "rollback-abort: wp-config.php checksum mismatch after restore (want $_want, got $_got) — restore incomplete/corrupt, manual recovery required" >&2
        exit 93
    fi
fi
# 5c. Recreate the transient dirs that were excluded from the archive. The
#     wipe in step 3 removed them and the extract in step 4 did not bring them
#     back, but wp-content/advanced-cache.php IS restored — so a cache drop-in
#     comes back live and expects its directory to exist. Most plugins recreate
#     it lazily; "most" is not a guarantee worth relying on in a rollback path.
{rollback_mkdir_line}
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

        # Layer 2: HTTP health. When the site's pre-mutation baseline was a
        # persistent 5xx (a Coming-Soon / archive plugin serving 503 by
        # design), the same status post-mutation is the *expected* healthy
        # state, not a regression.
        accept_5xx: set[int] | None = None
        if r.baseline_http_status and r.baseline_http_status >= 500:
            accept_5xx = {r.baseline_http_status}

        result = self._http_check(r.domain, accept_5xx=accept_5xx)
        if result != "ok":
            raise HealthCheckError(result)

    def _capture_http_status(self, domain: str) -> int | None:
        """Return the HTTP status code from a single GET against the
        siteurl (tries https first, then http). Returns None on a
        connection-class failure. Used to snapshot a site's
        intentional-non-2xx baseline before any mutation."""
        schemes = (
            [domain] if domain.startswith(("http://", "https://"))
            else [f"https://{domain}", f"http://{domain}"]
        )
        for base in schemes:
            try:
                req = urlrequest.Request(
                    base, headers={"User-Agent": "wp-update/1.0 (maintenance)"},
                )
                with urlrequest.urlopen(
                    req, timeout=self.args.http_timeout, context=self._ssl_ctx,
                ) as resp:
                    return resp.status
            except urlerror.HTTPError as exc:
                return exc.code
            except OSError:
                continue
        return None

    # Retry transient connection errors before declaring a site unhealthy.
    # 5xx and fatal-marker matches are deterministic and never retried.
    HTTP_RETRY_BACKOFFS = (0, 1.0, 2.0)

    def _http_check(
        self, domain: str, *, accept_5xx: set[int] | None = None,
    ) -> str:
        """
        Hit the site over HTTPS (fallback to HTTP) and check for 5xx
        status codes or fatal error markers in the response body.
        Returns "ok" or a description of the problem.

        `accept_5xx` is the set of 5xx status codes that should be treated
        as healthy (typically `{baseline_http_status}` for sites that
        intentionally serve a 5xx splash page).
        """
        schemes = (
            [domain] if domain.startswith(("http://", "https://"))
            else [f"https://{domain}", f"http://{domain}"]
        )
        last_err = "all HTTP checks failed"

        for base in schemes:
            for suffix in ("", "/wp-login.php"):
                url = f"{base}{suffix}"
                outcome = self._http_check_one(url, accept_5xx=accept_5xx)
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

    def _http_check_one(
        self, url: str, *, accept_5xx: set[int] | None = None,
    ) -> str | None:
        """Probe a single URL with retries for transient errors.

        Returns:
            None — passed; check the next suffix on the same scheme.
            "transient:<msg>" — transient failure exhausted retries; the
                                caller should try the next scheme.
            anything else — definitive failure description; caller bails.
        """
        last_exc: Exception | None = None
        last_5xx: str | None = None
        last_5xx_status: int | None = None
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
                        last_5xx_status = resp.status
                        continue
                    body = resp.read(65536).decode("utf-8", errors="ignore").lower()
                    for marker in FATAL_MARKERS:
                        if marker in body:
                            return f"{url} → fatal marker: {marker!r}"
                return None
            except urlerror.HTTPError as exc:
                if exc.code >= 500:
                    last_5xx = f"{url} → HTTP {exc.code}"
                    last_5xx_status = exc.code
                    continue
                # 3xx/4xx are deterministic — pass and check next suffix.
                return None
            except OSError as exc:
                # Includes URLError, socket.timeout, ConnectionRefusedError,
                # name resolution failures. Worth retrying.
                last_exc = exc
                continue

        # Exhausted retries. A repeated 5xx is now definitive (caller bails),
        # *unless* it matches the pre-mutation baseline — see _verify's
        # accept_5xx for the SeedProd-style intentional-503 case.
        if last_5xx is not None:
            if accept_5xx and last_5xx_status in accept_5xx:
                self.log.debug(
                    "HTTP %d matches baseline_http_status — accepting as "
                    "healthy: %s", last_5xx_status, last_5xx,
                )
                return None
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
        # Reuse one TCP+auth handshake for every command to the same host.
        # Without this, a slow Cloudways host (e.g. PAM/reverse-DNS stalls)
        # makes every wp-cli call pay a fresh-connection penalty (60-120s
        # observed on 45.76.170.254 during a sick-host incident).
        if not self.args.no_ssh_mux:
            common_opts.extend([
                "-o", "ControlMaster=auto",
                "-o", "ControlPath=~/.ssh/cm-%C",
                "-o", f"ControlPersist={self.args.ssh_mux_persist}",
            ])
        # Siteground requires port 18765; Cloudways uses default 22.
        # Pass -p only when non-default to keep ssh argv minimal on CW.
        if r.ssh_port and r.ssh_port != 22:
            common_opts.extend(["-p", str(r.ssh_port)])

        # load_env already expanded ~ in any path read from .env, so this
        # is just a typesafe Path() coercion.
        key_path = Path(r.ssh_key_path) if r.ssh_key_path else None
        # `-i` adds an identity but does not stop OpenSSH from offering every
        # key exposed by SSH_AUTH_SOCK. Hosts commonly disconnect after only a
        # handful of rejected keys, so a busy desktop agent can prevent the
        # requested key (or password fallback) from ever being attempted.
        key_auth_opts = [
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
        ]
        password_auth_opts = [
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password,keyboard-interactive",
        ]

        # Tier 2: SSH key + master username
        if r.auth_method == "master-key":
            target = f"{r.master_user}@{r.server_ip}"
            if key_path and key_path.exists():
                return ([
                    "ssh", *common_opts, *key_auth_opts,
                    "-i", str(key_path), target, "bash", "-ls",
                ], None)
            raise SSHError(f"master-key auth requires SSH key but {key_path} not found")

        # Tier 3: sshpass + master password (via SSHPASS env)
        if r.auth_method == "master":
            target = f"{r.master_user}@{r.server_ip}"
            return ([
                "sshpass", "-e",
                "ssh", *common_opts, *password_auth_opts,
                target, "bash", "-ls",
            ], r.master_password)

        # Tier 1 (default): SSH key + wpupdates user
        target = f"{r.ssh_user}@{r.server_ip}"
        if key_path and key_path.exists():
            return ([
                "ssh", *common_opts, *key_auth_opts,
                "-i", str(key_path), target, "bash", "-ls",
            ], None)

        # Password fallback for tier 1 (when no key file exists)
        if r.ssh_password and shutil.which("sshpass"):
            return ([
                "sshpass", "-e",
                "ssh", *common_opts, *password_auth_opts,
                target, "bash", "-ls",
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
        # Baseline reads (plugin/theme list, core check-update) all return a
        # JSON array. Use the same noise-tolerant scanner as the update paths
        # so a stray PHP notice or plugin-emitted line around the array does
        # not turn a healthy site into a false pre-flight failure.
        entries = _extract_wpcli_json_array(raw)
        if entries is not None:
            return entries
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WPCliError(f"Bad JSON from `wp {wp_cmd}`: {exc}") from exc

    def _flush_cache(self, r: SiteReport, after_slug: str) -> None:
        """Flush page+object cache after a plugin update.

        Uses the capability-mapped command when a known cache plugin is
        active; otherwise runs `wp cache flush` (WP core object cache,
        always available even without a cache plugin).

        Failures are logged and swallowed — a flush failure must never
        fail the plugin update step. Stale cache after one update is a
        soft regression; a verify failure on the NEXT update is the hard
        one we're protecting against.
        """
        caps = r.capabilities
        flush_cmd = (caps.cache_flush_cmd
                     if caps and caps.cache_flush_cmd else "cache flush")
        try:
            self._wp(r, flush_cmd, timeout=self.args.remote_timeout)
        except (SSHError, WPCliError) as exc:
            self.log.info("  ⤼ cache flush after %s skipped: %s  |  %s",
                          after_slug, exc, r.domain)

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

    def _maybe_update_sheet(self) -> None:
        """Update the 'Plugin Updates' Google Sheet (or whichever sheet
        --update-sheet points at) for every site that this run verified as
        current — i.e. needs_update=False after the run. That covers two
        equivalent end states:

          * overall='success'   — updates were applied and verified
          * overall='skipped'   — inline/cached check found nothing pending

        Skip reasons that don't set needs_update (WooCommerce gate, staging
        gate, --skip-recent dedupe, staging-cascade) are intentionally
        excluded so the sheet only reflects sites we actually confirmed.

        No-op unless --update-sheet (or UPDATE_SHEET_ID in .env) is set and
        we're in execute mode."""
        spreadsheet_id = (getattr(self.args, "update_sheet", "") or "").strip()
        if not spreadsheet_id:
            spreadsheet_id = (self.env.get("UPDATE_SHEET_ID", "") or "").strip()
        if not spreadsheet_id or not self.args.execute:
            return

        verified_domains = [
            r.domain for r in self.reports
            if r.needs_update is False and r.domain
        ]
        if not verified_domains:
            self.log.info(
                "Sheet update skipped: no sites ended in a verified-current "
                "state (needs_update=False)",
            )
            return

        try:
            import sheet_update as _sheet
        except ImportError as exc:  # pragma: no cover - defensive
            self.log.warning("Sheet update unavailable (%s) — skipping", exc)
            return

        from datetime import date as _date
        _sheet.update_sheet_for_successes(
            spreadsheet_id=spreadsheet_id,
            tab_name=getattr(self.args, "update_sheet_tab", "Plugin Updates"),
            success_domains=verified_domains,
            today=_date.today(),
            gws_path=getattr(self.args, "gws_path", "gws"),
            dry_run=getattr(self.args, "update_sheet_dry_run", False),
            log=self.log,
        )

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

        # Cache-dominated tree. No score penalty — the backup now excludes it,
        # so it doesn't make THIS run riskier — but a site more than half
        # regenerable cache has a problem the operator should see on the
        # dry-run report rather than discovering as a hard disk-check failure.
        excluded_mb = disk.get("excluded_mb", 0)
        if site_mb > 0 and excluded_mb > site_mb * 0.5:
            factors.append(
                f"  0  Note: {excluded_mb:.0f} MB of {site_mb:.0f} MB is "
                f"transient cache (excluded from backup)"
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
            excluded_mb = disk.get("excluded_mb", 0)
            if excluded_mb:
                L("  │                %s MB transient cache excluded "
                  "→ %s MB actually archived",
                  f"{excluded_mb:.0f}",
                  f"{disk.get('backed_up_mb', 0):.0f}")
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
            # Branch order matters: the pre-flight up-to-date skip fires
            # BEFORE the WooCommerce gate in _process_site, so for a
            # WooCommerce + up-to-date site the real reason for the skip
            # is "no updates available", not "WooCommerce — manual review".
            # Check needs_update=False first so the glyph reflects reality.
            if r.overall == "skipped" and r.needs_update is False:
                extra = "  [up to date — skipped]"
            elif r.overall == "skipped" and r.has_woocommerce:
                extra = "  [WooCommerce — manual review]"
            elif r.overall in ("failed", "rolled-back"):
                extra = f"  [failed at: {r.failure_step}]"
            elif r.overall == "dry-run":
                conf = r.baseline.get("confidence", {})
                bits = []
                if conf:
                    bits.append(f"{conf['grade']} {conf['score']}/100")
                if r.needs_update is True:
                    bits.append("needs update")
                elif r.needs_update is False:
                    bits.append("up to date")
                if bits:
                    extra = "  [" + " · ".join(bits) + "]"

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
        help=(
            "Directory with *_cloudways.json or *_siteground.json files "
            f"(default: {DEFAULT_CLIENTS})"
        ),
    )
    p.add_argument(
        "--provider", choices=("auto", "cloudways", "siteground"),
        default="auto",
        help=(
            "Filter which provider's client files to process when scanning "
            "--clients-dir. 'auto' (default) processes both. Per-file "
            "schema detection still drives runtime behavior; this only "
            "affects the file glob."
        ),
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
        "--no-backup-excludes", action="store_true",
        help=(
            "Archive transient cache directories too (larger backups, and a "
            "cache-bloated site may fail the disk check). By default "
            f"{len(BACKUP_EXCLUDE_DIRS)} regenerable dirs such as "
            "wp-content/cache are excluded from both the backup and the "
            "disk-space estimate."
        ),
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
        "--no-ssh-mux", action="store_true",
        help="Disable SSH connection multiplexing (ControlMaster). Each "
             "command will open a fresh TCP+auth handshake — slower, "
             "especially on hosts where sshd login is slow.",
    )
    p.add_argument(
        "--ssh-mux-persist", default="5m",
        help="ControlPersist duration for SSH multiplexing (default: 5m). "
             "Master connection lingers this long after last use.",
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
        "--skip-up-to-date-ttl", type=int, default=60, metavar="MINUTES",
        help="In execute mode, skip sites whose latest dry-run summary "
             "(within the last N minutes) reported needs_update=false. "
             "Default: 60. Set to 0 to disable summary-driven skipping. "
             "Stale summaries are ignored — only sites with a fresh "
             "dry-run inside the TTL are eligible for the skip.",
    )
    p.add_argument(
        "--no-skip-up-to-date", action="store_true",
        help="Disable summary-driven 'no updates available' skipping for "
             "this run. Equivalent to --skip-up-to-date-ttl 0 but kept "
             "as an explicit flag for clarity in run logs.",
    )
    p.add_argument(
        "--recheck-updates", action="store_true",
        help="In execute mode, ignore prior dry-run summaries and instead "
             "SSH into each site, collect a fresh baseline, then skip if "
             "no core/theme/plugin update is available. Costs one round "
             "of WP-CLI calls per site but is always current. Useful "
             "when you don't trust the most recent dry-run.",
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
    p.add_argument(
        "--update-sheet", default="", metavar="SPREADSHEET_ID",
        help="At end of an execute run, update this Google Sheet's 'Next "
             "Update' (col B → next Monday) and 'Last Updated' (col C → "
             "today) columns for every site this run verified as current "
             "— either a successful update OR an auto-skip that confirmed "
             "needs_update=False. Sites skipped by the WooCommerce gate, "
             "staging gate, --skip-recent dedupe, or a staging-cascade are "
             "left alone. Matches sheet col E (wp-admin URL) to SiteReport "
             "domain. Failed and rolled-back sites are never written. "
             "Requires `gws` CLI auth (run `gws auth login` once). Falls "
             "back to UPDATE_SHEET_ID in .env if not passed. Disabled by "
             "default.",
    )
    p.add_argument(
        "--update-sheet-tab", default="Plugin Updates", metavar="TAB_NAME",
        help="Tab name in the spreadsheet to update (default: 'Plugin Updates').",
    )
    p.add_argument(
        "--update-sheet-dry-run", action="store_true",
        help="With --update-sheet, log the rows that would be written but "
             "do not call the Sheets batchUpdate API.",
    )
    p.add_argument(
        "--gws-path", default="gws", metavar="PATH",
        help="Path to the `gws` (Google Workspace CLI) binary used by "
             "--update-sheet (default: `gws` on PATH).",
    )
    return p.parse_args()


def main() -> int:
    args = build_cli()
    updater = WPUpdater(args)
    return updater.run()


if __name__ == "__main__":
    raise SystemExit(main())
