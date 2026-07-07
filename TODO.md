# TODO

## Refactor: split `wp_update.py` into a package

**Status:** not started
**Why:** `wp_update.py` is now 2,214 lines in a single file — CLI parsing,
SSH transport, wp-cli wrapper, backup/rollback orchestration, confidence
scoring, summary writer, and DB ingest all live in one module. It works,
but it's hard to navigate, hard to unit-test in isolation, and grows every
time we fix a corner case (six bug fixes landed today alone).

`webui.py` is heading the same direction (1,703 lines). `db.py` is the
only file currently on a healthy trajectory.

**Target shape (sketch — not final):**

```
wpmaint/
├── __init__.py
├── cli.py              # argparse + main() (was build_cli/main in wp_update.py)
├── config.py           # .env loading, path defaults, constants
├── inventory.py        # client JSON loading + validation (was _validate_app etc.)
├── ssh.py              # _ssh / _wp / _wp_json + auth tier cascade
├── backup.py           # pre-flight backup, archive verify, cleanup
├── rollback.py         # _step_rollback + recovery (deactivate-on-fatal)
├── steps/
│   ├── core.py         # core update + update-db
│   ├── themes.py       # theme update loop
│   └── plugins.py      # sequential plugin update + per-plugin snapshot
├── verify.py           # _verify (wp core is-installed + HTTP + fatal markers)
├── confidence.py       # confidence scoring + grading
├── reports.py          # SiteReport, StepResult, summary writer
├── runner.py           # WPUpdater orchestrator (the run() loop)
├── db.py               # (unchanged — already a clean module)
└── webui/              # split webui.py the same way
    ├── app.py
    ├── auth.py
    ├── runs.py
    └── ...
```

**Migration approach (do not big-bang):**

1. Land this DB integration first (done 2026-04-28) — keeps the diff narrow.
2. Pure extractions before behavioral changes. Each PR moves one concern out
   with no logic delta; tests must keep passing.
3. Order by lowest cross-coupling: `config.py`, `reports.py`,
   `confidence.py`, `verify.py`, then `ssh.py`, then the step modules.
4. `WPUpdater` becomes a thin orchestrator that wires injected modules.
5. After `wp_update.py` is split, do the same to `webui.py`.

**Constraints to preserve:**

- The CLI entrypoint must stay at `wp_update.py` (skills, the webui, and
  user muscle memory all invoke it by name). Make `wp_update.py` a thin
  shim that calls into `wpmaint.cli.main()`.
- `db/wpmaint.db` schema and `wp-update-summary-<run_id>.json` shape must
  not break — the webui reads both.
- Test layout (`tests/test_wp_update.py`, `tests/test_webui.py`,
  `tests/test_db.py`) can be reorganized but coverage must not regress.

**Out of scope for this refactor:**

- Async/concurrent site processing.
- Pulling in third-party deps (paramiko/fabric, click, sqlalchemy).
  Stdlib + sshpass remains the rule.
