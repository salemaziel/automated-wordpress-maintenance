# AGENTS.md

This repository is a Python project for automating Wordpress maintenance on remote web hosting.
It contains a single file Python CLI (`wp_update.py`), a simple WebUI (`webui.py`), and lightweight local tooling for linting and tests.

*Note:* Currently Cloudways specific; other providers to be added soon.

## Key Files

| File | Purpose |
|---|---|
| `.env` | `SSH_USER`, `APP_PW`, `SSH_KEY`, `SSH_USER_CANDIDATES` credentials consumed during provisioning |
| `scripts/convert_cloudways.py` | Parses the text manifest into per-client JSON files |
| `clients/` | `convert_cloudways.py` output directory — one `<slug>_cloudways.json` per client server |
| `logs/` | Default log output directory — 2 logs produced, `wp-update-<date>.log` and `wp-update-summary-<date>.json` |
| `wp_update.py` | Primary automation script |
| `webui.py` | WebUI |

## Commands

```bash
# Convert text manifest to JSON files 
# WARNING: wipes and regenerates clients.
python3 scripts/convert_cloudways.py
```

```bash
# Runs the automation update script in **dry run mode** using .env file and clients 
# directory in project's root directory **(default behavior)**
python3 wp_update.py --env-file ./.env --clients-dir ./clients --log-dir ./logs

# Runs the automation update script in **dry run mode** using .env file and clients 
# directory in project's root directory with streaming logs for realtime log monitoring 
# **(default for webUI)**
python3 wp_update.py --env-file ./.env --clients-dir ./clients --stream

# Runs the automation update script in **dry run mode** using default settings for .env file
# running on a single client's file from provider Cloudways **(indicated by file ending in
# `_cloudways.json`)** with streaming logs for realtime log viewing
python3 wp_update.py --client-file ./clients/test_juha_staging_cloudways.json --stream

# Runs the automation update script in **dry run mode** using default settings for .env file  
# running on all files in clients/ directory **including client sites marked** `woocommerce: true`
python3 wp_update.py --clients-dir ./clients --include-woocommerce --stream

# Runs the automation update script in execute mode using default settings for .env file  
# running on all non-Woocommerce sites in clients/ directory, with streaming logs` 
python3 wp_update.py --clients-dir ./clients --execute --stream

# Shows all flags available for runtime configurations (e.g. ENV_FILE location, CLIENT_DIR 
# or CLIENT_FILE location, LOG_DIR location, skipping staging sites, skipping SSL verification,
# setting SSH_CONFIG, CONNECT_TIMEOUT, REMOTE_TIMEOUT, HTTP_TIMEOUT, MAX_CONSECUTIVE_FAILURES)
python3 wp_update.py --help
```

### WebUI

WebUI file: `webui.py`
```bash
WEBUI_USERNAME=admin
WEBUI_PASSWORD=testpass
```
**To Do: Add WebUI documentation here**


## Client JSON Schema

(Output Schema of `scripts/convert_cloudways.py`)

Every client file follows this structure. 
SFTP credentials are generally masked as literal `$SSH_USER`, `$APP_PW`, `$SSH_KEY`.
After 1st script run on client file, `$SSH_USER` may be replaced by a static string
indicating the correct ssh user so as to skip testing other possibilities.

```json
{
  "client_name": "...",
  "email": "...",
  "server_ip_address": "...",
  "master_credentials": { "username": "...", "password": "..." },
  "applications": [{
    "website_domain": "...",
    "path_to_public_html": "/home/master/applications/<dir>/public_html",
    "sftp_credentials": { "username": "$SSH_USER", "password": "$APP_PW", "ssh_key": "$SSH_KEY" },
    "environment_flags": { "wp_cli_installed": true, "is_staging": false, "has_woocommerce": false }
  }]
}
```

## Known Limitations

- **SSH host key verification is TOFU.** `wp_update.py` connects with `StrictHostKeyChecking=accept-new`, which trusts whatever host key the server presents on first contact. A MITM at first contact would not be detected. For higher assurance, pre-populate `~/.ssh/known_hosts` with each Cloudways server's host key (e.g. `ssh-keyscan -H <ip> >> ~/.ssh/known_hosts`) and switch the option to `StrictHostKeyChecking=yes`.
- **Currently limited to clients using Cloudways webhost as provider. Other providers to include soon are Siteground, Pressable, and Cloudron.**
- **Backups are not byte-complete.** Transient directories listed in `BACKUP_EXCLUDE_DIRS` (page cache, WP's upgrade staging) are excluded from `public_html.tar.gz`, so a rollback restores the site without its cache — WordPress and the cache plugin regenerate it. `wp-content/wflogs` (Wordfence state) and `wp-content/updraft` (the client's own backups) are deliberately **kept**. Pass `--no-backup-excludes` for a full archive.
- **Orphaned backups are never reaped, and the master user cannot delete them.** When a client file is archived out of `clients/`, any `wp-maintenance-backups/<run_id>/` directories left on its server are no longer visited by any run. Worse, they are *unreapable* by the master account: backups are written as the **application** user, and the run dirs land mode 755 owned by that app user, while the master user is only in group `www-data` (`r-x`). Unlinking needs write on the containing directory, so master-side cleanup fails with "Permission denied" on every file, 100% of the time. Confirmed on `143.244.179.152` (2026-07-30): `master_ybawsmbccn` uid 1003 fails; the app-scoped SSH account `wpupdates` (uid 1006, same uid as the app user `vkkueyverz`) succeeds. The app user itself has `/usr/sbin/nologin` and cannot be used directly. Any future cleanup must run over the app-scoped SSH account, or the backup routine must create these dirs group-writable (2775). 2.7 GB of such orphans were removed manually on this one server.
- **Disk triage as the app SSH user under-reports.** Sibling applications on the same Cloudways server are unreadable to an app-scoped user, so `du` over `/home` silently undercounts (4.9 GB vs 7.8 GB actual on `143.244.179.152`). Use master credentials when investigating server-wide disk usage.
