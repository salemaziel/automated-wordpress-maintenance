# Cloudways WordPress Maintenance Automation

Safely updates WordPress core, themes, and plugins across multiple Cloudways client sites from a single local command. The project is designed for cautious operations: dry-run first, pre-flight backups before any mutation, health checks after each update step, and automatic rollback when a site becomes unhealthy.

## Overview

This repository contains a single Python CLI, `wp_update.py`, plus inventory/config examples. It is meant for batch maintenance across many Cloudways-hosted WordPress sites where the operator wants one repeatable workflow instead of logging into each app manually.

The automation is optimized for production safety:

- **Dry-run by default** so the first pass collects facts without changing remote systems
- **Per-site baseline capture** for WordPress version, PHP version, pending updates, disk headroom, and detected backup plugins
- **Sequential plugin updates** with verification after each plugin so failures are isolated to a specific step
- **Pre-flight backup + rollback** using both a database export and a filesystem archive
- **Credential-safe reporting** so passwords and key paths are not written to the JSON summary

## What the project does

For each application listed in the client inventory, the script:

1. Validates the inventory entry and target WordPress path
2. Establishes SSH access and confirms `wp-cli` can reach the installation
3. Collects a baseline of core/theme/plugin state
4. Estimates backup size and checks available disk space
5. In execute mode, creates a pre-flight SQL dump and `public_html` archive
6. Updates core, themes, and plugins
7. Verifies site health after each critical change
8. Rolls the site back if a step fails
9. Writes a run log and a machine-readable summary JSON

## Repository contents

| Path | Purpose |
|------|---------|
| `wp_update.py` | The entire maintenance CLI and orchestration logic |
| `pyproject.toml` | Configuration for `pytest` and `ruff` |
| `requirements-dev.txt` | Development dependencies for linting and tests |
| `tests/test_wp_update.py` | Unit tests for helper logic and core non-network behavior |
| `.env.example` | Example local environment file for SSH credentials |
| `clients/example-client_cloudways.json` | Example client inventory file |
| `.github/copilot-instructions.md` | Repository-specific guidance for future Copilot sessions |

## Requirements

### Local machine

- Python 3
- Standard SSH client (`ssh`)
- Network access to the Cloudways servers over SSH
- Network access to target site domains for post-update HTTP health checks
- Optional: `sshpass` if password-based fallback auth is required

This project has no checked-in runtime Python package dependencies; it uses the standard library. Development tooling lives in `requirements-dev.txt`.

### Remote environment

- A working WordPress install in a Cloudways application directory
- `wp-cli` available in the remote login shell
- A target path shaped like `/home/master/applications/<app-id>/public_html`

## Configuration

### 1. Create a local `.env`

Start from `.env.example` and create a real `.env` in the repository root:

```bash
cp .env.example .env
```

Example contents:

```bash
export SSH_KEY=~/.ssh/cloudways_wpupdates
export SSH_USER=wpupdates
export APP_PW=
```

The script's `.env` parser accepts shell-style lines such as:

- `export KEY=value`
- `KEY=value`
- quoted values like `KEY="value"`

`SSH_KEY`, `SSH_USER`, and `APP_PW` are the shared defaults used when the client inventory points at `$SSH_KEY`, `$SSH_USER`, or `$APP_PW`.

### 2. Create client inventory files

Client files live in `clients/` and must be named `*_cloudways.json`.

Example structure:

```json
{
  "client_name": "Example Client",
  "email": "owner@example.com",
  "server_ip_address": "203.0.113.10",
  "master_credentials": {
    "username": "master_xxxxx",
    "password": "..."
  },
  "applications": [
    {
      "website_domain": "example.com",
      "path_to_public_html": "/home/master/applications/abcd1234/public_html",
      "sftp_credentials": {
        "username": "$SSH_USER",
        "password": "$APP_PW",
        "ssh_key": "$SSH_KEY"
      },
      "environment_flags": {
        "wp_cli_installed": true,
        "is_staging": false,
        "has_woocommerce": false
      }
    }
  ]
}
```

Important inventory rules:

- `path_to_public_html` must match the Cloudways application path format exactly
- `sftp_credentials` values can be literal strings or `$ENV_VAR` placeholders resolved from `.env`
- `is_staging` and `has_woocommerce` materially change behavior
- the script tolerates incomplete or malformed client files by logging and skipping them instead of aborting the whole run

## How authentication works

SSH access is attempted in a fallback sequence:

1. **App-scoped SSH user + key**
2. **Master user + same key**
3. **Master user + password via `sshpass`**

If the script has to operate as the master user, it captures the original filesystem ownership before updates and restores it afterward.

## How the update flow works

`wp_update.py` is intentionally monolithic: all orchestration lives in one file so the full update lifecycle is visible in one place.

### High-level flow

1. `build_cli()` parses flags and defaults
2. `main()` constructs `WPUpdater`
3. `WPUpdater.run()` discovers inventory files and processes them one by one
4. `_process_client_file()` validates each application and, in execute mode, sorts staging sites before production sites
5. `_process_site()` performs the per-site lifecycle:
   - SSH preflight
   - baseline collection
   - disk-space check
   - backup planning/creation
   - core update
   - theme updates
   - plugin updates
   - final verification
   - rollback on failure

### Why plugin updates are one-at-a-time

Plugin updates are the riskiest part of a WordPress maintenance run. Instead of bulk-updating all plugins, the script updates one plugin, verifies the site, records the result, then moves to the next one. That makes it clear which exact plugin caused a regression.

### How verification works

Verification is two-layered:

1. `wp core is-installed` via `wp-cli`
2. HTTP checks against the site root and `/wp-login.php`

The HTTP verification treats 5xx responses and known fatal-error markers in the response body as failures.

### How rollback works

Before any mutation in execute mode, the script creates:

- `preflight.sql` using `wp db export --add-drop-table`
- `public_html.tar.gz` containing the full WordPress filesystem

If a later step fails:

1. the broken state is archived
2. `public_html` is cleared and restored from the tarball
3. the database is restored from `preflight.sql`
4. the site is verified again

Backups are stored on the remote server under:

```text
/home/master/wp-maintenance-backups/<client-slug>/<app-id>/<run-id>/
```

## Usage

### Inspect the CLI

```bash
python3 wp_update.py --help
```

### Dry-run all clients

This is the default mode and the safest first step.

```bash
python3 wp_update.py
```

Dry-run mode still performs:

- inventory validation
- SSH preflight
- baseline collection
- disk-space checks
- confidence scoring

It does **not** create backups or update anything remotely.

### Dry-run one client file

```bash
python3 wp_update.py --client-file clients/example-client_cloudways.json
```

### Execute against all client files

```bash
python3 wp_update.py --execute
```

### Execute against one client file

```bash
python3 wp_update.py --execute --client-file clients/example-client_cloudways.json
```

### Include WooCommerce sites

WooCommerce sites are skipped by default for safety.

```bash
python3 wp_update.py --execute --include-woocommerce
```

### Skip staging sites

```bash
python3 wp_update.py --skip-staging
```

### Stream full live output

By default, stdout is informational while the file log captures DEBUG detail. Use `--stream` to mirror the detailed SSH and command output to stdout.

```bash
python3 wp_update.py --execute --stream
```

### Use custom paths

```bash
python3 wp_update.py \
  --env-file /path/to/.env \
  --clients-dir /path/to/clients \
  --log-dir /path/to/logs
```

## CLI options

| Flag | Purpose |
|------|---------|
| `--execute` | Perform live updates instead of dry-run |
| `--env-file` | Load credentials from a non-default `.env` |
| `--clients-dir` | Process all `*_cloudways.json` files from a different directory |
| `--client-file` | Limit the run to one client inventory file |
| `--log-dir` | Write logs and summaries to a different directory |
| `--include-woocommerce` | Allow WooCommerce sites to be updated |
| `--skip-staging` | Skip apps marked with `is_staging=true` |
| `--skip-ssl-verify` | Disable certificate verification for HTTP health checks |
| `--connect-timeout` | SSH connection timeout in seconds |
| `--remote-timeout` | Per-remote-command timeout in seconds |
| `--http-timeout` | HTTP health-check timeout in seconds |
| `--stream` | Show DEBUG-level activity on stdout |

## Output and reporting

Each run writes two local artifacts under `logs/` by default:

- `wp-update-<run_id>.log`
- `wp-update-summary-<run_id>.json`

The summary JSON includes:

- run metadata (`run_id`, mode, timestamp)
- total site counts
- per-status totals (`success`, `dry_run`, `skipped`, `rolled_back`, `failed`)
- per-site baseline data and step history

Per-site status values are:

- `dry-run`
- `success`
- `skipped`
- `rolled-back`
- `failed`

## Operational conventions

### Dry-run is the normal first pass

The project is built around running a dry-run first, reviewing confidence scores and pending updates, then deciding whether to execute.

### Staging sites go first

In execute mode, staging sites are processed before production sites. If a staging site fails or rolls back, the script skips remaining production sites from that same client file.

### WooCommerce requires explicit opt-in

Sites marked `has_woocommerce=true` are treated as higher risk and skipped unless `--include-woocommerce` is passed.

### Confidence scores are part of the workflow

Dry-run reports include a confidence score and grade:

- `HIGH`
- `MEDIUM`
- `LOW`
- `RISKY`

The score is influenced by factors such as pending plugin/theme/core updates, disk space, PHP version, site size, backup tooling, and whether the site is staging.

## Development and maintenance

Set up a local virtual environment before running lint or tests:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

The most relevant local checks are:

```bash
# Syntax check
python3 -m py_compile wp_update.py

# Inspect CLI changes
python3 wp_update.py --help

# Lint
.venv/bin/python -m ruff check .

# Run all tests
.venv/bin/python -m pytest

# Run one test
.venv/bin/python -m pytest tests/test_wp_update.py::test_validate_app_resolves_placeholders_from_env
```

## Common gotchas

- A missing `.env` causes startup failure because credentials are loaded immediately
- `sshpass` is only needed for password fallback; key-based auth is the normal path
- The remote WordPress path must be the Cloudways `public_html` path, not a parent directory
- Health checks hit both the site root and `/wp-login.php`, so firewall, DNS, or certificate issues can fail verification even if SSH succeeds
- `wp-cli` must be available in the remote login shell because the script runs remote commands with `bash -ls`
- On systems with PEP 668 enabled, install dev tooling into `.venv` instead of the system Python
