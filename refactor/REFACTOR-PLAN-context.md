# REFACTOR-PLAN — Supporting Context

Two checklists from the pre-plan exploration that the plan references but doesn't enumerate.

## 1. Tests that call `wp_update.py` private names — Phase A0 checklist

These tests directly import and call private members. Every Track-A extraction phase must migrate the relevant subset in the same PR so the test suite stays green at every phase boundary.

| Test file:line | Calls | Migrates with phase |
|---|---|---|
| `tests/test_wp_update.py:102` | `SiteReport.to_dict()` credential omission | A2 (models) |
| `tests/test_wp_update.py:175, 770` | `updater._validate_app(...)`, `updater._process_client_file(...)` | A4 (inventory) |
| `tests/test_wp_update.py:202` | `updater._validate_app(...)` | A4 (inventory) |
| `tests/test_wp_update.py:231, 253, 244, 267` | `updater._compute_confidence(...)` | A5 (confidence) |
| `tests/test_wp_update.py:673, 700` | `updater._step_rollback(...)` | A5 (rollback) |
| `tests/test_wp_update.py:903-957` (7 tests) | `updater._run_plugin_update_structured(...)` | A3 (wpcli) |
| `tests/test_wp_update.py:971-1127` (5 tests) | `updater._step_update_plugins(...)` indirectly via `run()` | A5 (steps) |
| `tests/test_wp_update.py:785` | `updater._process_client_file(...)` | A4 (inventory) |

`tests/test_db.py:31-32` calls `db._apply_schema()` — out of scope for this refactor program (db.py untouched).

There is no `conftest.py`. Fixtures are inline (`make_args()`, `make_report()`, `valid_client()` builders + `memory_db` fixture in `test_webui.py:257`).

## 2. webui.py route inventory — Track B Phase B1 checklist

Confirmed by exploration. 18 endpoints. Auth/CSRF columns drive `Depends()` decisions in B3.

| Path | Method | Auth | CSRF | Handler |
|---|---|---|---|---|
| `/login` | GET | – | – | serves `LOGIN_HTML` |
| `/logout` | GET | – | – | clears session, redirects |
| `/` | GET | ✓ | – | serves `app_html()` |
| `/api/login` | POST | – | – | sets session + CSRF cookies |
| `/api/clients` | GET | ✓ | – | lists client files (`?provider=`) |
| `/api/clients/{name}/history` | GET | ✓ | – | client run history |
| `/api/clients/import` | POST | ✓ | ✓ | imports client JSON |
| `/api/clients/manual` | POST | ✓ | ✓ | creates client manually |
| `/api/runs` | GET | ✓ | – | lists runs (`?limit=&client=&status=`) |
| `/api/runs` | POST | ✓ | ✓ | starts run via `start_run()` |
| `/api/runs/{id}/stream` | GET | ✓ | – | SSE — `_stream_run()` (B4 hot path) |
| `/api/runs/{id}/summary` | GET | ✓ | – | run summary JSON |
| `/api/runs/{id}/cancel` | POST | ✓ | ✓ | `cancel_run()` |
| `/api/ssh-keys` | GET | ✓ | – | lists SSH keys |
| `/api/ssh-keys` | POST | ✓ | ✓ | uploads SSH key |
| `/api/stats/plugins` | GET | ✓ | – | plugin failure stats |
| `/api/logs` | GET | ✓ | – | lists log files |
| `/api/logs/{filename}` | GET | ✓ | – | streams log file (raw bytes) |

Provider gate: `start_run()` rejects non-Cloudways with `ValueError` → HTTP 400 (`webui.py:569` and `webui.py:239`).

Subprocess invocation: `_run_process()` at `webui.py:730` spawns `subprocess.Popen` from `record.command` (built by `build_local_command()` / `build_remote_command()`); reads stdout line-by-line and publishes to `record.listeners` queues.

## 3. wp_update.py `_step_*` method roster — Phase A5 checklist

Nine step methods to extract as module-level functions in `updater/steps.py`:

1. `_step_ssh_preflight` — `wp_update.py:1015`
2. `_step_capture_ownership` — `wp_update.py:1139`
3. `_step_restore_ownership` — `wp_update.py:1161`
4. `_step_collect_baseline` — `wp_update.py:1176`
5. `_step_disk_check` — `wp_update.py:1224`
6. `_step_backup` — `wp_update.py:1283`
7. `_step_update_core` — `wp_update.py:1337`
8. `_step_update_themes` — `wp_update.py:1359`
9. `_step_update_plugins` — `wp_update.py:1458`

`_step_rollback` (`wp_update.py:1624`) is conditional on failure — extract to `updater/rollback.py` separately. Heredoc body lives at `wp_update.py:1632-1683`.
