# Copilot instructions for this repository

See [AGENTS.md](../AGENTS.md) for key files, client JSON schema, and known limitations. See [CLAUDE.md](../CLAUDE.md) for non-obvious gotchas (webui env vars, SSH config quirk, ruff scoping).

## Commands

```bash
# Create a local dev environment for linting/tests
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt

# Syntax check
python3 -m py_compile wp_update.py

# Inspect the CLI and defaults
python3 wp_update.py --help

# Lint — scope to changed files to avoid noise from untracked support dirs
.venv/bin/python -m ruff check wp_update.py webui.py db.py sheet_update.py

# Run the full test suite (covers wp_update, webui, db, sheet_update)
.venv/bin/python -m pytest

# Run a single test
.venv/bin/python -m pytest tests/test_wp_update.py::test_validate_app_resolves_placeholders_from_env

# Dry-run a single Cloudways client file
python3 wp_update.py --client-file clients/cloudways/example-client/example-client_cloudways.json

# Dry-run a single Siteground client file
python3 wp_update.py --client-file clients/siteground/example.com/example.com_siteground.json

# Live run for one client file
python3 wp_update.py --execute --client-file clients/cloudways/amy/amy_sandiegoharpist.com_cloudways.json

# Dry-run all clients (both providers)
python3 wp_update.py

# Dry-run only Cloudways or Siteground clients
python3 wp_update.py --provider cloudways
python3 wp_update.py --provider siteground

# Backfill the SQLite DB from existing summary JSON files
.venv/bin/python scripts/backfill_db.py

# Start the Web UI
env WEBUI_USERNAME=admin WEBUI_PASSWORD=testpass WEBUI_SECRET=testsecret \
  python3 webui.py --host 127.0.0.1 --port 8787
```

The script defaults to `.env`, `clients/`, `logs/`, and `db/wpmaint.db` under the repository root. Start by creating a real `.env` from `.env.example` and pointing `--client-file` at a concrete inventory file to limit scope.

## Project layout

| File | Purpose |
|---|---|
| `wp_update.py` | Main automation CLI — all update, backup, rollback logic |
| `webui.py` | Web UI (stdlib `http.server`) for triggering and monitoring runs |
| `db.py` | SQLite persistence layer (`db/wpmaint.db`) — run history, deduplication |
| `sheet_update.py` | Google Sheets post-run hook — writes next/last update dates |
| `scripts/convert_cloudways.py` | Converts text manifest → per-client Cloudways JSON files |
| `scripts/backfill_db.py` | One-shot backfill of `db/wpmaint.db` from existing `logs/` summaries |
| `clients/cloudways/<slug>/` | One JSON file per Cloudways client (`*_cloudways.json`) |
| `clients/siteground/<domain>/` | One JSON file per Siteground client (`*_siteground.json`) |
| `clients/pressable/` | Placeholder — runner not yet implemented |
| `tests/` | `test_wp_update.py`, `test_webui.py`, `test_db.py`, `test_sheet_update.py` |

## High-level architecture

`wp_update.py` owns the update pipeline. `build_cli()` parses flags, `main()` constructs `WPUpdater`, and `WPUpdater.run()` discovers client inventory files (both `*_cloudways.json` and `*_siteground.json` by default), processes them sequentially, then writes `wp-update-summary-<run_id>.json` plus `wp-update-<run_id>.log` under `logs/` and ingests results into `db/wpmaint.db`.

The main control flow is:

1. `_process_client_file()` loads one provider JSON, validates required fields, builds a `SiteReport` per application, and in execute mode sorts staging sites before production sites.
2. `_process_site()` runs the per-site pipeline: SSH preflight, baseline collection, disk-space check, backup planning/creation, then in execute mode ownership capture, core update, theme updates, plugin updates, final verification, and rollback on failure.
3. `_step_update_plugins()` is intentionally atomic: plugins are updated one at a time and `_verify()` runs after each update so the exact failing plugin is known.
4. `_step_rollback()` restores both filesystem and database from the pre-flight backup, then re-verifies the site. Backup location is provider-specific: Cloudways stores under `<app_dir>/private_html/wp-maintenance-backups/<run_id>/`; Siteground stores under `<home>/wp-maintenance-backups/<run_id>/`.

Remote execution is a core design choice. `_ssh()` pipes multi-line shell scripts to `ssh ... bash -ls` over stdin instead of building large quoted SSH command strings. SSH connection multiplexing (ControlMaster) is enabled by default; disable with `--no-ssh-mux`. `_wp()` always `cd`s into `wp_path` before invoking `wp --path=...` because Cloudways installs can rely on relative includes from the WordPress root.

## Key conventions

- **Dry-run is the default.** Only `--execute` is allowed to mutate remote systems. Dry-runs still perform inventory validation, baseline collection, disk checks, and confidence scoring.
- **Multi-provider support.** Client files are auto-detected by filename suffix (`*_cloudways.json`, `*_siteground.json`). Use `--provider {auto,cloudways,siteground}` to restrict which provider's files are scanned. Siteground runner is fully implemented; Pressable is a placeholder.
- **`clients/` is now provider-scoped.** Files live under `clients/cloudways/<slug>/` and `clients/siteground/<domain>/`. `--client-file` is repeatable to process a subset.
- **Credential indirection is expected.** `sftp_credentials.username/password/ssh_key` in client JSON can be literal values or `$ENV_VAR` placeholders resolved from `.env`. The `.env` parser accepts shell-style lines such as `export SSH_KEY=...`.
- **Authentication is a three-tier fallback.** SSH preflight first tries the app-scoped SSH user/key, then master user with the same key, then `sshpass` with the master password. If master auth is used, ownership capture and restore are part of the normal update flow.
- **`sshpass` is optional, but only for the last-resort path.** Key-based auth is the normal case; password auth is only attempted when key auth fails and master credentials are present.
- **WooCommerce is gated by default.** Sites with `has_woocommerce=true` are skipped unless `--include-woocommerce` is passed.
- **Staging-first is intentional.** In execute mode, staging sites run before production, and a staging failure or rollback causes remaining production sites from the same client file to be skipped.
- **Verification is two-layered.** `_verify()` combines `wp core is-installed` with HTTP checks against the site and `wp-login.php`, including fatal-error marker detection in response bodies.
- **Idempotent reruns via SQLite.** `--skip-recent HOURS` (default: 24) skips sites that already succeeded in an execute-mode run within the window, querying `db/wpmaint.db` first, falling back to `logs/wp-update-summary-*.json`. Disable with `--skip-recent 0` or `--no-db`.
- **Step history is the canonical audit trail.** Per-site state should be recorded through `SiteReport` and `_record_step()` rather than ad hoc prints. The summary file is built from `SiteReport.to_dict()`, which intentionally excludes runtime credentials.
- **Confidence scoring is part of the dry-run contract.** Dry-runs are not just connectivity checks; they populate baseline data and a risk score/grade that the operator is expected to review before live execution.
- **Logs are UTC and `--stream` changes stdout verbosity only.** File logs always capture DEBUG output; `--stream` mirrors that detail to stdout for live troubleshooting.
- **Google Sheets post-run hook.** Pass `--update-sheet <spreadsheet_id>` to write next/last update dates after a successful execute run. Shells out to `gws sheets`; requires the `gws` CLI on PATH (override with `--gws-path`).
