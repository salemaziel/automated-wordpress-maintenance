# Refactor Program — WordPress Maintenance Toolkit

## Context

IMPORTANT: Read `REFACTOR-PLAN-context.md` for primary context file.

Five refactor briefs in `refactor/1..5` collectively describe a transition from three god-files (`wp_update.py` 2,421 lines, `webui.py` 2,032 lines, `db.py` 876 lines) to a modular toolkit with capability-aware update orchestration and a FastAPI web layer. The briefs were written iteratively — each only knew about the ones before it — so phase numbers, ordering, and code samples in those docs are visionary guidance rather than step-by-step instructions. This plan uses them as inputs and re-sequences based on dependency, risk, and what the exploration found in the actual code.

**Why the work is needed.** The current `WPUpdater` god class mixes SSH transport, WP-CLI, inventory loading, step orchestration, and reporting in a single class — none of which is unit-testable without subprocess mocking. `webui.py` is hand-rolled `BaseHTTPRequestHandler` with HTML/CSS/JS embedded as f-strings. The runner has no application-layer health visibility and no awareness of caching plugins, which directly causes false-positive HTTP verification failures (cached error pages outliving the actual fix).

**Outcome targeted.** Module-level testability of every step. FastAPI web layer with proper SSE. `wp doctor` blocks updates when core checksums fail. Cache flush runs after every plugin update so the verify step sees ground truth. Plugin-capability detection drives backup, cache, maintenance-mode, and Woo-aware behavior.

---

## Approach Decisions (deviations from the docs)

1. **Two parallel tracks, not one queue.** Track A (wp_update.py + features) and Track B (webui FastAPI port) touch disjoint files. Doc 1 explicitly says `wp_update.py` and `db.py` stay untouched during the FastAPI port. Run them on parallel branches so calendar time isn't wasted.

2. **Pre-refactor safety net is its own phase A0.** Doc 2 calls the rollback heredoc "sacred" but the test suite never asserts the `exit 99/98/97/96` guards exist. Add that assertion before any structural change so every subsequent phase has it as a regression boundary.

3. **Tests that call private names are migrated as part of each extraction phase, not after.** The exploration found 6+ tests directly importing `updater._validate_app`, `updater._compute_confidence`, `updater._step_rollback`, `updater._run_plugin_update_structured`, `updater._process_client_file`. The docs don't acknowledge this. Each extraction phase ships its test migration in the same PR so green tests are a continuous invariant.

4. **Doc 2 Phase 7 (`RunConfig` dataclass) is folded into the Phase 6 slim-runner work or skipped.** Doc 2 itself flags it "optional, low priority" — it's noise that should be decided once the call surface is stable, not separately planned.

5. **Doc 1 phase ordering is changed: auth-as-Depends moves before SSE migration.** Doc 1 sequences SSE before auth-deps, but SSE handlers need auth checks. Doing auth-deps first means the SSE migration writes against the final auth surface instead of porting verbatim and then refactoring the same line twice.

6. **Doc 4 is split.** Doc 4 itself recommends only `wp profile stage` for the runner; the full `hook`/`queries` surface belongs in the FastAPI UI. The plan treats them as two separate deliverables on the two tracks.

7. **Doc 5 (capabilities) lands AFTER Track A's step extraction (A5), not woven into earlier phases.** Doc 5 adds new steps and a new `SiteReport` field — easier to land cleanly when steps already live in `updater/steps.py`.

8. **Code samples in the briefs are not authoritative.** Function signatures, dataclass fields, and exact error handling are designed during implementation, not transcribed from the briefs.

9. **Out of scope (explicitly deferred):**
   - `db.py` → SQLModel/ORM migration (Doc 1 §Phase 6 says "deferred, separate PR" — keep it deferred)
   - Extracting `wp_update.py` logic into the webui's service layer — the subprocess contract is the integration boundary and that's correct
   - Doc 4's interactive profile UI happens only after Track B Phase 5 ships and is its own follow-on

---

## Track A — `wp_update.py` modularization + feature integrations

Each phase ships as its own PR. The full test suite stays green at every phase boundary. Logic is preserved byte-for-byte through phases A1–A6; A7–A9 are purely additive.

### A0 — Safety net (NEW, pre-refactor)

**Goal:** lock in the invariants the refactor must not break.

- Add a test asserting the rendered rollback script string contains each of `exit 99`, `exit 98`, `exit 97`, `exit 96`. Test sits next to the existing `test_step_rollback_success_constructs_correct_script` in `tests/test_wp_update.py`.
- Add a test calling `SiteReport.to_dict()` on a fully-populated report (all credential fields set) and asserting `ssh_password`, `master_password`, `ssh_key_path` are absent.
- Audit `tests/test_wp_update.py` for every `updater._<private>(...)` call site and record the list in the PR description so phase-by-phase test migration has a checklist.

### A1 — Extract constants and exceptions

Create `core/constants.py` (module-level constants only — VALID_PATH, FATAL_MARKERS, KNOWN_BACKUP_PLUGINS, CONFIDENCE_RULES, the `_PLUGIN_STATUS_*` frozensets) and `core/exceptions.py` (the five exception classes at `wp_update.py:262-279`). `wp_update.py` re-imports them. No call-site changes elsewhere.

**Critical files:** `wp_update.py:85-150`, `wp_update.py:262-279`.

### A2 — Extract models and small helpers

Create `core/models.py` with `StepResult`, `SiteReport` (and its `to_dict()`), plus `ts()` and `slugify()` helpers. Do not change field shapes. `SiteReport.to_dict()` is a contract consumed by `db.py` ingest and by `webui.py` — any rename here forces a coordinated webui change, so don't.

**Critical files:** `wp_update.py:179-255` (dataclasses), `wp_update.py:286-288, 400-401` (helpers).

### A3 — Extract SSH transport and WP-CLI abstraction

Create `ssh/transport.py` and `ssh/wpcli.py`. The transport class takes connection-policy fields (timeouts, ssh_config path, skip-ssl flag) at construction time; per-call site info comes in via `SiteReport`. The `_is_permission_denied` heuristic moves with the transport.

The WPCli class wraps the transport and centralizes the `cd <path> && wp --path=<path>` prefix, JSON parsing with empty-output handling, and the plugin-update structured retry loop (the retry-on-empty + retry-on-no-entry logic at `wp_update.py:1404-1457`). That retry logic is the most subtle code in the file — copy it verbatim into the new method, then add unit tests against a fake transport that exercise both retry paths and a tolerant-status path.

**Testability unlock:** every step in subsequent phases can be tested by passing a fake `SSHTransport` whose `execute()` returns canned strings.

**Critical files:** `wp_update.py:1832-1973` (transport + wpcli surface), `wp_update.py:999-1013` (permission-denied heuristic), `wp_update.py:1404-1457` (plugin retry).

### A4 — Extract inventory layer

Create `inventory/loader.py`. Move `load_env`, `load_client_notes`, `_normalize_domain`, `skip_items_for_domain`, `resolve`, `_gather_client_files`, `_validate_app`, and the load-and-validate part of `_process_client_file`. The dedupe (`_load_recent_successes` lookup) and staging-first sort stay in `runner.py` because they're orchestration, not loading.

**Critical files:** `wp_update.py:291-401` (helpers), `wp_update.py:547-577` (gather), `wp_update.py:634-844` (process + validate).

### A5 — Extract update steps, verify, rollback, confidence

Largest phase by line count, lowest by logic risk because every step becomes a module-level function with an explicit `(report, wpcli, args)` (or similar narrow) signature.

- `updater/verify.py` — `_verify`, `_http_check`, `_http_check_one`, `HTTP_RETRY_BACKOFFS`, the SSL context construction.
- `updater/rollback.py` — `_step_rollback`. Pull the heredoc (currently `wp_update.py:1632-1683`) into a named module-level constant `ROLLBACK_SCRIPT_TEMPLATE`. The A0 exit-code test now points at this constant.
- `updater/confidence.py` — `_compute_confidence` becomes a pure function `compute_confidence(report) -> dict`.
- `updater/steps.py` — the nine `_step_*` functions: `_step_ssh_preflight`, `_step_capture_ownership`, `_step_restore_ownership`, `_step_collect_baseline`, `_step_disk_check`, `_step_backup`, `_step_update_core`, `_step_update_themes`, `_step_update_plugins`. The atomic plugin loop with deactivate-on-fatal stays inside `_step_update_plugins` — do not split it.

**Critical files:** `wp_update.py:1015-1709` (steps + rollback), `wp_update.py:1720-1831` (verify + http), `wp_update.py:2023-2140` (confidence).

### A6 — Slim down `WPUpdater` orchestrator

`runner.py` holds a thin `WPUpdater` class that owns: env, logger, transport+wpcli construction, DB handle, recent-success cache, circuit-breaker state, and the `_process_site` orchestration spine.

`_process_site` stays as one method at `wp_update.py:845-998`. The doc warns against turning it into a chain-of-responsibility pipeline; that warning stands. The explicit sequential step calls with named `current_step` tracking is the audit trail.

`wp_update.py` at the repo root becomes an entry-point shim of ~30 lines: imports `build_cli` and `WPUpdater`, runs them. The CLI surface (every flag in `build_cli()`) is unchanged — `webui.py` calls this script as a subprocess and depends on those flags.

**Decision on Doc 2 Phase 7 `RunConfig`:** if `_process_site` and the step functions end up reading `args.execute`, `args.remote_timeout`, `args.http_timeout`, `args.skip_ssl_verify`, etc. through long attribute chains, fold a frozen `RunConfig` dataclass into A6. Otherwise skip — `argparse.Namespace` is fine for a tool with one entry point.

### A7 — Capability detection (Doc 5)

Add `step_detect_capabilities` as the second step after SSH preflight, before backup. It runs `wp plugin list --status=active --format=json` once and builds a `SiteCapabilities` object that records: which backup plugin (if any) is present and has CLI surface, which page-cache plugin is present and its flush command, whether a security plugin is active, whether maintenance-mode coordination is needed.

`SiteReport` gets a `capabilities: SiteCapabilities | None` field. Subsequent steps consult it instead of re-detecting. Concrete behaviors that change:

- **Backup step** picks UpdraftPlus CLI when present and remote storage is configured; otherwise tar fallback (current behavior). The decision is logged either way; tar always still runs as the rollback artifact when remote storage isn't confirmed.
- **Cache flush** runs after every plugin update inside the atomic loop, using the capability-mapped command (W3TC / WP Super Cache / LiteSpeed / WP Rocket / WP Fastest Cache / fallback to `wp cache flush`). This is the highest-leverage win — it eliminates the false-positive verify failures caused by stale cache pages.
- **Maintenance mode** wraps the update window for sites where capabilities flag it as appropriate. `_verify` is skipped during the maintenance window and runs once after deactivation.
- **WooCommerce** sites still gated by `--include-woocommerce`, but when included, run `wp wc system-status --format=json` pre and post and surface the delta.

The `KNOWN_BACKUP_PLUGINS` constant from `core/constants.py` already lists the plugin slugs to detect — extend that scope to include cache, security, and maintenance plugin slugs.

### A8 — `wp doctor` integration (Doc 3)

Add `run_doctor()` and a `DoctorResult` dataclass to `updater/verify.py`. Two integration points:

- **Pre-flight:** run `wp doctor check --all --format=json` after capability detection, before backup. Hard-block execution when any check in a small allowlist returns `error` status — minimum: `core-verify-checksums`, `php-in-upload`, `file-eval`. A blocked run records the doctor result in the SiteReport and exits the site with a clear status (not a crash).
- **Post-update:** run a focused subset (`cron-count`, `cron-duplicates`, `autoload-options-size`) and compare to the pre-flight snapshot. New `error` results trigger rollback; new `warning` results log a delta and continue.

Graceful degradation: if `wp doctor` is not installed, `WPCliError` is caught and the entire feature is skipped with a single log warning. No new opt-in flag for now — the brief's `--install-doctor` flag is deferred until usage data shows it's needed.

`DOCTOR_HARD_BLOCKS` lives in `core/constants.py`.

### A9 — `wp profile stage` delta (Doc 4 partial)

Smallest of the feature additions. Add `snapshot_profile_stages()` to `updater/verify.py`. Snapshot pre-flight into `SiteReport.baseline["profile_stages"]`, compare post-update; flag stage-level slowdowns past a configurable threshold (default 30%) as a `performance_regression: bool` on the report. **Never triggers rollback** — performance is too noisy a signal. Same graceful-degradation pattern as doctor.

`PROFILE_REGRESSION_THRESHOLD` lives in `core/constants.py`.

The interactive `wp profile hook` / `wp profile queries` surface stays out of Track A and lands as a Track B follow-on (B7) once the FastAPI SSE infrastructure exists.

---

## Track B — `webui.py` FastAPI port

Independent track. Touches `webui.py` and creates a new `app/` package. Does not modify `wp_update.py`, `db.py`, or the subprocess contract. Phase order differs from Doc 1 — auth-as-deps moves before SSE for the reason in Decision 5.

### B1 — Framework scaffold

Create `app/main.py`, `app/routers/`, `app/core/`. FastAPI app with a `lifespan` that calls the same `db.init_db` / `db.sweep_orphan_running` / `db.reconcile_pending_ingests` startup sequence the current `webui.py` runs.

Port each `do_GET` / `do_POST` branch from `WebUIHandler` (`webui.py:957`, `webui.py:1028`) into a corresponding router file. Confirmed routes from exploration: 18 endpoints across login, clients, runs, ssh-keys, stats, logs. Logic inside each route is copy-pasted verbatim — including the inline auth/CSRF checks. Goal of this phase is structural only.

Validation: run the old server on 8787 and the new one on 8788; `curl` each endpoint with the same payloads and diff JSON response bodies.

### B2 — Template extraction

`LOGIN_HTML` (`webui.py:1271-1309`, 39 lines) becomes `app/templates/login.html.j2`. `app_html()` (`webui.py:1309-1995`, ~686 lines) becomes `app/templates/index.html.j2`. The ~500 lines of inline JavaScript inside `app_html` move to `app/static/app.js`. Mount static via `StaticFiles`.

Care points: Jinja2 auto-escapes HTML, so any `{{ var }}` that interpolates into a JavaScript literal needs `| tojson`. `{% raw %}` blocks wrap any literal that contains template-like braces in the inline JS.

### B3 — Auth and CSRF as `Depends`

Move `sign_session`, `verify_session`, `session_csrf`, and the per-IP login rate-limit logic from procedural functions (`webui.py:157-204`) into `Depends()` callables in `app/core/auth.py`. Routes declare `username = Depends(require_auth)` and POSTs add `_ = Depends(require_csrf)`.

Cookie name, signature scheme, TTL, SameSite, Path stay byte-identical so existing browser sessions survive the cutover.

### B4 — SSE migration (highest technical risk)

The current `_stream_run` (`webui.py:1196`) feeds output via `threading.Lock` + `queue.Queue` and writes raw to `wfile`. The migration uses `asyncio.Queue` per request and bridges the worker thread to the event loop with `loop.call_soon_threadsafe(queue.put_nowait, line)`.

`RunRecord` gets a small parallel listener interface (`add_async_listener` / `remove_async_listener`) that lives alongside the existing thread-queue listener. The thread that reads `subprocess.Popen.stdout` (`webui.py:730`) calls every registered listener (sync queues for any leftover consumers, async-safe callbacks for FastAPI). Both delivery models coexist — a hard cutover is unnecessary and risky.

The replay-then-stream pattern stays: the SSE generator first yields the `RunRecord.lines` buffer, then awaits the async queue. Cancel and finish events thread through unchanged.

**Do not** swap `subprocess.Popen` + `threading.Thread` for `asyncio.create_subprocess_exec` here. That's a separate optimization that's not load-bearing for the FastAPI port.

### B5 — Pydantic request models

Replace the manual `str(payload.get(...) or "")` extraction in `build_wp_args`, `validate_client_doc`, and `start_run` with Pydantic models. Field names match what the existing front-end JS sends — no JS changes required. FastAPI's automatic 422 with field-level errors replaces the scattered `raise ValueError(...)` guards.

### B6 — Settings migration and uvicorn entry point

`Settings` dataclass (`webui.py:55`) becomes a `pydantic-settings` `BaseSettings` with `env_prefix="WEBUI_"`. Same env var names. Server bootstrap moves from `ThreadingHTTPServer` to `uvicorn.run("app.main:app", ...)`. The `--host` / `--port` CLI flags survive.

### B7 — Profile UI (Doc 4 follow-on, optional)

After B5 ships, add a per-site profile panel: SSE-streamed `wp profile stage bootstrap --spotlight --format=json` and `wp profile queries --time_threshold=0.05 --format=json`. Reuses the SSE infrastructure built in B4. No runner changes — these calls go through a new `/api/profile/<site>` endpoint that runs `wp_update.py --profile-only` (a new flag) or directly SSHs given the SiteReport context. Decide which during B7.

---

## Cross-track dependencies

There is exactly one: **`SiteReport.to_dict()` shape**. The webui reads it from `db.py` (which ingested it from a runner summary). If Track A changes a field name in `SiteReport.to_dict()`, Track B sees breakage on history endpoints. The mitigation is the rule "no field renames during the refactor" already encoded in Doc 2's safety rules — every track-A phase preserves the dict shape.

If Track A's A7 (capabilities) adds a `capabilities` field to `SiteReport.to_dict()`, Track B optionally adds a column or section that displays it. Coordinate this in the A7 PR description.

---

## Verification per phase

After every Track-A phase:

1. `python3 -m py_compile wp_update.py`
2. `.venv/bin/python -m ruff check wp_update.py core/ ssh/ inventory/ updater/ reporting/`
3. `.venv/bin/python -m pytest`
4. `python3 wp_update.py --help` (CLI surface unchanged)
5. Dry-run against `clients/example-client_cloudways.json` and confirm the summary JSON has the same shape as before the phase

After every Track-B phase:

1. Both servers running in parallel; for every endpoint in the route inventory, `curl -b cookie.txt http://localhost:8787/<path>` and `http://localhost:8788/<path>` and `diff` the JSON bodies (or the rendered HTML key fragments)
2. Login flow end-to-end in a browser (cookie set, app HTML loads, CSRF token populates)
3. Start a dry run from the UI, watch SSE stream, confirm the run shows in `/api/runs` and `/api/runs/<id>/summary` returns the right shape
4. Cancel a running dry-run, confirm SIGTERM and `cancelled` status

---

## Critical files

- `wp_update.py:85-150` — constants → A1
- `wp_update.py:179-279` — models + exceptions → A1, A2
- `wp_update.py:1404-1457` — plugin update retry → A3 (high subtlety)
- `wp_update.py:1632-1683` — rollback heredoc → A5 (named constant)
- `wp_update.py:845-998` — `_process_site` orchestration spine → preserved in A6 as one method
- `webui.py:55-204` — settings, session, CSRF → B1, B3, B6
- `webui.py:957-1170` — handler dispatch → B1
- `webui.py:1196-1265` — `_stream_run` SSE → B4
- `webui.py:1271-1995` — login HTML, app HTML, inline JS → B2
- `tests/test_wp_update.py` — every phase migrates the test calls that match the names being moved
- `core/constants.py` (new) — landing pad for `KNOWN_*`, `DOCTOR_HARD_BLOCKS`, `PROFILE_REGRESSION_THRESHOLD`, `CONFIDENCE_RULES`

## Risks and mitigations

| Risk | When | Mitigation |
|---|---|---|
| Rollback shell-script whitespace change breaks remote execution | A5 | A0 added the exit-code regression test; named `ROLLBACK_SCRIPT_TEMPLATE` constant makes any diff visually obvious |
| Plugin-update retry logic silently lost during transport extraction | A3 | Copy verbatim; unit-test all three retry paths against a fake transport before declaring A3 done |
| Tests that import `updater._private` break across phase boundaries | every A phase | Each phase's PR migrates its own private-name test calls; A0 records the full list as a checklist |
| `SiteReport.to_dict()` field rename leaks to webui | any A phase | Treat dict shape as a contract; any addition is additive only and called out in the PR |
| FastAPI SSE blocks the event loop | B4 | `loop.call_soon_threadsafe` bridge; do not run blocking code inside the async generator |
| Session cookie behavior changes during port | B3 | Keep cookie name, path, max-age, SameSite values identical to the current `webui.py` constants |
| `wp doctor` / `wp profile` not installed on remote | A8, A9 | `WPCliError` caught → feature skipped with a log warning; never blocks normal updates |
| Cache flush command fires against a plugin that's not actually installed | A7 | Capability detection drives the choice; fallback to `wp cache flush` (always available) when nothing matches |
| Webui shows stale `runs` schema after `SiteReport.to_dict()` adds capabilities field | A7 / B-track | Coordinate in A7 PR description; treat as a Track-B follow-up issue, not a blocker |

---

## What ships when

- A0 + A1 + A2: small mechanical PRs, days each.
- A3: the centerpiece of the refactor. Adds the most testability. ~1 week.
- A4 + A5 + A6: the bulk of the move. ~2 weeks combined.
- A7: capability detection + cache flush is the highest-impact feature change. ~1 week.
- A8 + A9: additive, ~3 days each.
- B1 + B2 + B3: foundational FastAPI work. ~1 week combined.
- B4: SSE bridge, the highest-risk Track-B work. ~3-4 days alone with careful testing.
- B5 + B6: mechanical. Days each.
- B7: only after B5; bounded by what UI affordances Todd's operators need.

The first PR is **A0** — pure test additions, no production code change. Second is **A1**. Track B can start at any time and runs in parallel.
