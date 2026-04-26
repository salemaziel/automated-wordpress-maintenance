# CLAUDE.md

Read `.github/copilot-instructions.md` for full commands and architecture.
Read `AGENTS.md` for key files, schema, and known limitations.
This file captures non-obvious gotchas for AI sessions only.

## Web UI (`webui.py`)

Required env vars: `WEBUI_USERNAME`, `WEBUI_PASSWORD`, `WEBUI_SECRET`.
Optional remote vars: `WEBUI_REMOTE_HOST`, `WEBUI_REMOTE_USER`, `WEBUI_REMOTE_REPO_PATH`, `WEBUI_REMOTE_PORT`, `WEBUI_REMOTE_IDENTITY`.
Startup: `env WEBUI_USERNAME=admin WEBUI_PASSWORD=testpass WEBUI_SECRET=testsecret python3 webui.py --host 127.0.0.1 --port 8787`
Provider tabs (Cloudways/Siteground/Cloudron) exist in the UI but only Cloudways has a runner; others return 400.
Uploaded SSH keys are stored in `.webui-keys/` (gitignored, dir 0700, files 0600).

## SSH Config Quirk

`wp_update.py` defaults to `-F /dev/null` to avoid a broken system SSH config
(`/etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf` has bad permissions on this machine).
Use `--ssh-config <path>` to opt into a custom config. Use `--ssh-key <path>` to override `SSH_KEY` from `.env`.

## Linting

Repo-wide `ruff check .` is noisy due to generated/untracked support directories.
Scope to changed files: `.venv/bin/python -m ruff check webui.py tests/test_webui.py`

## Hooks (active)

PostToolUse: ruff auto-runs `--fix` on any `.py` file after Edit/Write — no manual lint needed after edits.
PreToolUse: editing `.env` is blocked (holds live SSH credentials). Edit manually in a terminal, or pass `--ssh-key <path>` to `wp_update.py` instead.

## Skills

`/run-maintenance` — interactive prompt that constructs and runs `wp_update.py` with the right flags.
`/read-logs` — parses `logs/` and summarizes the latest run: outcomes, failures, rollbacks, follow-ups needed.
