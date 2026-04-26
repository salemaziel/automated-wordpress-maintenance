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
python3 wp_update.py --client-dir ./clients --include-woocommerce --stream

# Runs the automation update script in execute mode using default settings for .env file  
# running on all non-Woocommerce sites in clients/ directory, with streaming logs` 
python3 wp_update.py --client-dir ./clients --execute --stream

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
