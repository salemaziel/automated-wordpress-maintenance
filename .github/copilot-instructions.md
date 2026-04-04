# Copilot instructions for this repository

## Commands

This repository is a single-file Python CLI with lightweight local tooling for linting and tests.

```bash
# Create a local dev environment for linting/tests
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt

# Syntax check
python3 -m py_compile wp_update.py

# Inspect the CLI and defaults
python3 wp_update.py --help

# Lint
.venv/bin/python -m ruff check .

# Run the full test suite
.venv/bin/python -m pytest

# Run a single test
.venv/bin/python -m pytest tests/test_wp_update.py::test_validate_app_resolves_placeholders_from_env

# Dry-run a single inventory file from this checkout
python3 wp_update.py --client-file clients/example-client_cloudways.json

# Live run for one client file
python3 wp_update.py --execute --client-file clients/example-client_cloudways.json

# Dry-run all inventory files in clients/
python3 wp_update.py
```

The script defaults to `.env`, `clients/`, and `logs/` under the repository root. This checkout ships `.env.example` plus `clients/example-client_cloudways.json`, so local runs usually start by creating a real `.env` and then pointing `--client-file` at a concrete inventory file when you want to limit scope.

## High-level architecture

`wp_update.py` owns the whole system. `build_cli()` parses flags, `main()` constructs `WPUpdater`, and `WPUpdater.run()` discovers client inventory files, processes them sequentially, then writes `wp-update-summary-<run_id>.json` plus `wp-update-<run_id>.log` under `logs/`.

The main control flow is:

1. `_process_client_file()` loads one `*_cloudways.json`, validates required fields, builds a `SiteReport` per application, and in execute mode sorts staging sites before production sites.
2. `_process_site()` runs the per-site pipeline: SSH preflight, baseline collection, disk-space check, backup planning/creation, then in execute mode ownership capture, core update, theme updates, plugin updates, final verification, and rollback on failure.
3. `_step_update_plugins()` is intentionally atomic: plugins are updated one at a time and `_verify()` runs after each update so the exact failing plugin is known.
4. `_step_rollback()` restores both filesystem and database from the pre-flight backup stored in `/home/master/wp-maintenance-backups/...`, then re-verifies the site.

Remote execution is a core design choice. `_ssh()` pipes multi-line shell scripts to `ssh ... bash -ls` over stdin instead of building large quoted SSH command strings. `_wp()` always `cd`s into `wp_path` before invoking `wp --path=...` because Cloudways installs can rely on relative includes from the WordPress root.

## Key conventions

- **Dry-run is the default.** Only `--execute` is allowed to mutate remote systems. Dry-runs still perform inventory validation, baseline collection, disk checks, and confidence scoring.
- **Inventory contract matters.** Client files must be named `*_cloudways.json`; `path_to_public_html` must match `/home/master/applications/<hash>/public_html`; `environment_flags` drives behavior such as `is_staging` and `has_woocommerce`.
- **Credential indirection is expected.** `sftp_credentials.username/password/ssh_key` in client JSON can be literal values or `$ENV_VAR` placeholders resolved from `.env`. The `.env` parser accepts shell-style lines such as `export SSH_KEY=...`.
- **Authentication is a three-tier fallback.** SSH preflight first tries the app-scoped SSH user/key, then master user with the same key, then `sshpass` with the master password. If master auth is used, ownership capture and restore are part of the normal update flow.
- **`sshpass` is optional, but only for the last-resort path.** Key-based auth is the normal case; password auth is only attempted when key auth fails and master credentials are present.
- **WooCommerce is gated by default.** Sites with `has_woocommerce=true` are skipped unless `--include-woocommerce` is passed.
- **Staging-first is intentional.** In execute mode, staging sites run before production, and a staging failure or rollback causes remaining production sites from the same client file to be skipped.
- **Verification is two-layered.** `_verify()` combines `wp core is-installed` with HTTP checks against the site and `wp-login.php`, including fatal-error marker detection in response bodies.
- **Step history is the canonical audit trail.** Per-site state should be recorded through `SiteReport` and `_record_step()` rather than ad hoc prints. The summary file is built from `SiteReport.to_dict()`, which intentionally excludes runtime credentials.
- **Confidence scoring is part of the dry-run contract.** Dry-runs are not just connectivity checks; they populate baseline data and a risk score/grade that the operator is expected to review before live execution.
- **Logs are UTC and `--stream` changes stdout verbosity only.** File logs always capture DEBUG output; `--stream` mirrors that detail to stdout for live troubleshooting.
