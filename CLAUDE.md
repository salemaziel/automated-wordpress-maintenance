# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Browser-automation provisioning toolkit for Todd's WordPress clients hosted on Cloudways. Two workflows produce the same output:

1. **Live browser extraction** — Chrome MCP navigates the Cloudways web console, provisions SSH/SFTP access, whitelists IPs, and extracts server details into per-client JSON files.
2. **Offline conversion** — `scripts/convert_cloudways.py` parses the flat-text manifest (`todd-clients-cloudways.txt`) into the same JSON schema.

Both paths write to `clients/<slug>_cloudways.json`. The JSON schema is defined in `example-client_cloudways.json`.

## Key Files

| File | Purpose |
|---|---|
| `AGENTS.md` | Full browser-automation workflow (Phases 0-3); read this to understand the provisioning steps |
| `.env` | `SSH_USER`, `APP_PW`, `SSH_KEY` credentials consumed during provisioning |
| `IP_WHITELIST.txt` | IPs to whitelist on each Cloudways server (one per line) |
| `cloudways_rsa.pub` | Public key deployed to each client's SSH/SFTP user |
| `todd-clients-cloudways.txt` | Flat-text manifest of all client records (source of truth for offline conversion) |
| `scripts/convert_cloudways.py` | Parses the text manifest into per-client JSON files |
| `clients/` | Output directory — one `<slug>_cloudways.json` per client server |

## Commands

```bash
# Convert text manifest to JSON files (wipes and regenerates clients/)
python3 scripts/convert_cloudways.py
```

No other build, lint, or test commands exist.

## JSON Output Schema

Every client file follows this structure. SFTP credentials are always masked as literal `$SSH_USER`, `$APP_PW`, `$SSH_KEY` — never raw values.

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

## Multi-Site Grouping Logic

Clients with multiple Cloudways applications (e.g., Jerry has `lifeworkseducation.com` + `jerrybridge.com`) are grouped into a single JSON file when they share the same server IP, master username, or master password. The converter's `compatible()` function enforces this — distinct servers for the same client name produce separate files with `-2`, `-3` suffixes (e.g., `alfredo_cloudways.json`, `alfredo-2_cloudways.json`).

## Browser Automation Notes

The live workflow (AGENTS.md) uses Chrome MCP tools (`mcp__claude-in-chrome__*`). Critical constraints:

- **Cloudways UI is slow** — always wait for spinners/toasts/DOM repaints after every click or form submission
- **Active client is hidden** from the dropdown — process the currently-loaded client first, then iterate through the dropdown
- **Shell access toggle** has high latency — poll DOM for success state before continuing
- Incomplete clients (no email, no IP, empty paths with spaces) exist in the manifest and need data filled via browser extraction

## Known Limitations

- **SSH host key verification is TOFU.** `wp_update.py` connects with `StrictHostKeyChecking=accept-new`, which trusts whatever host key the server presents on first contact. A MITM at first contact would not be detected. For higher assurance, pre-populate `~/.ssh/known_hosts` with each Cloudways server's host key (e.g. `ssh-keyscan -H <ip> >> ~/.ssh/known_hosts`) and switch the option to `StrictHostKeyChecking=yes`.

## Known Limitations

- **SSH host key verification is TOFU.** `wp_update.py` connects with `StrictHostKeyChecking=accept-new`, which trusts whatever host key the server presents on first contact. A MITM at first contact would not be detected. For higher assurance, pre-populate `~/.ssh/known_hosts` with each Cloudways server's host key (e.g. `ssh-keyscan -H <ip> >> ~/.ssh/known_hosts`) and switch the option to `StrictHostKeyChecking=yes`.
