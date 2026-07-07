# `wp_update.py` Refactor & Re-architecture Plan

**NOTE: This is for wp_update.py only. Refactoring for WebUI and db.py in other REFACTOR files.**

## Overview

Structure of the Plan
The core argument is that wp_update.py's logic is excellent — the SSH auth cascade, atomic plugin updates, rollback backstop guards, confidence scoring — all of it stays. What's wrong is that WPUpdater is a god class mixing 5 distinct concerns that can't be tested or reasoned about independently.

Target module layout:

- `core/` — models, exceptions, constants, config
- `ssh/` — SSHTransport (subprocess isolation) + WPCli (WP-CLI abstraction)
- `inventory/` — client JSON loading, env parsing, app validation
- `updater/` — _step_* functions, rollback, verify, confidence scoring
-`reporting/` — summary writing and print formatting
- `wp_update.py` entry point → ~30 lines

7 phases, each its own PR, strictly no logic changes until after the refactor is done. The biggest payoff is Phase 3 (SSH transport extraction) which unlocks unit testing all step logic without a live remote host by injecting a fake SSHTransport.

The hardest rule in the doc: the rollback shell script heredoc is sacred — extract it to a named constant and add a test asserting the exit 99/98/97/96 guards are present before touching anything near it.


## Current State Assessment

`wp_update.py` is a single-file, ~1,100-line monolith with a well-designed interior but a structural layout that will resist testing, extension, and reuse as the project grows. The logic is sound — the design principles, the three-tier SSH auth cascade, atomic plugin updates, rollback with backstop guards, confidence scoring — all of this should be preserved exactly. The problem is that everything lives in one class (`WPUpdater`) that mixes five distinct concerns:

| Concern | Current location | Problem |
|---|---|---|
| SSH transport + auth | `_ssh`, `_ssh_cmd`, `_step_ssh_preflight` | Untestable without a real host or complex subprocess mocking |
| WP-CLI abstraction | `_wp`, `_wp_text`, `_wp_json` | Coupled to `SiteReport` credentials |
| Inventory loading | `_gather_client_files`, `_validate_app`, `_process_client_file` | Buried in the runner |
| Business logic (update steps, rollback, verify) | `_step_*`, `_step_rollback`, `_verify` | Can't be tested without SSH |
| Reporting + output | `_print_site_report`, `_print_final_report`, `_write_summary` | Mixed with step execution |

The data classes (`SiteReport`, `StepResult`) and exceptions are already well-separated and require no changes.

---

## Target Architecture

```
wp_update.py                     ← entry point only: build_cli() + main()
core/
  __init__.py
  models.py                      ← SiteReport, StepResult (moved from wp_update.py)
  exceptions.py                  ← InventoryError, SSHError, HealthCheckError,
                                    WPCliError, RollbackFailed
  constants.py                   ← VALID_PATH, FATAL_MARKERS, KNOWN_BACKUP_PLUGINS,
                                    CONFIDENCE_RULES, _PLUGIN_STATUS_*
  config.py                      ← RunConfig (replaces argparse.Namespace throughout)
ssh/
  __init__.py
  transport.py                   ← SSHTransport class: _ssh_cmd, _ssh
  wpcli.py                       ← WPCli class: _wp, _wp_text, _wp_json,
                                    _run_plugin_update_structured
inventory/
  __init__.py
  loader.py                      ← load_env, _gather_client_files, _process_client_file,
                                    _validate_app, load_client_notes, skip_items_for_domain
updater/
  __init__.py
  steps.py                       ← All _step_* methods extracted as standalone functions
  rollback.py                    ← _step_rollback extracted
  verify.py                      ← _verify, _http_check, _http_check_one
  confidence.py                  ← _compute_confidence, CONFIDENCE_RULES reference
  runner.py                      ← WPUpdater (slim orchestrator only — no SSH, no HTTP)
reporting/
  __init__.py
  summary.py                     ← _write_summary, _print_final_report
  site_report.py                 ← _print_site_report, _print_site_execution_report
```

`wp_update.py` at the root becomes ~30 lines: imports `build_cli` and `WPUpdater`, that's it.

---

## What Does NOT Change

This is as important as what does change. The following must be preserved byte-for-byte in terms of behavior:

- **The three-tier SSH auth cascade** — tier 1 app-scoped candidates, tier 2 master-key, tier 3 sshpass+password. The `_is_permission_denied` heuristic stays.
- **Stdin piping** (`bash -ls`) instead of SSH positional args — this design is correct and must not regress.
- **Atomic plugin update loop** — one plugin at a time, verify after each, deactivate-to-isolate before full rollback.
- **Rollback backstop guards** — the `exit 99/98/97/96` checks in the rollback shell script must stay exactly as-is.
- **Credential serialization safety** — `SiteReport.to_dict()` must never include `ssh_password`, `master_password`, `ssh_key_path`.
- **Circuit breaker** — `_consecutive_execute_failures` / `--max-consecutive-failures`.
- **Staging-first ordering** and the staging-gate skip on production sites when staging fails.
- **All CLI flags** — `build_cli()` output is the public interface; none of the flags change.

---

## Phase-by-Phase Plan

### Phase 1 — Extract constants and exceptions (zero behavior change)

**Files created:** `core/constants.py`, `core/exceptions.py`

Move out of `wp_update.py`:
- `VALID_PATH`, `FATAL_MARKERS`, `KNOWN_BACKUP_PLUGINS`, `CONFIDENCE_RULES`
- `_PLUGIN_STATUS_SUCCESS`, `_PLUGIN_STATUS_UPTODATE`
- All five exception classes

`wp_update.py` imports them back. Tests pass unchanged because nothing moved that has behavior.

**Why first:** These have no dependencies. Extracting them unlocks everything else without creating circular imports. Easy to verify — `python3 -c "from core.exceptions import SSHError"` is the entire test.

---

### Phase 2 — Extract models (zero behavior change)

**Files created:** `core/models.py`

Move `StepResult`, `SiteReport` (including `to_dict`) and the helpers they use: `ts()`, `slugify()`.

`SiteReport` currently has runtime credentials as fields. Do **not** move those to a separate object yet — that's a Phase 5 concern. Keep the dataclass identical.

**Why second:** Models are imported by every other layer. Getting them out early prevents all future extraction from needing to import `wp_update.py` itself.

---

### Phase 3 — Extract SSH transport

**Files created:** `ssh/transport.py`, `ssh/wpcli.py`

**`SSHTransport`** takes `(ssh_key: str, connect_timeout: int, remote_timeout: int, ssh_config: Path, skip_ssl_verify: bool)` in its constructor — not a `SiteReport`. It receives a `SiteReport` only at call time for the per-call credential and host info.

```python
class SSHTransport:
    def __init__(self, connect_timeout: int, remote_timeout: int, ssh_config: Path): ...
    def execute(self, r: SiteReport, script: str, timeout: int | None = None) -> str: ...
    def build_cmd(self, r: SiteReport) -> tuple[list[str], str | None]: ...

    @staticmethod
    def is_permission_denied(stderr: str) -> bool: ...
```

**`WPCli`** wraps `SSHTransport` and knows about WP-CLI conventions: the `cd` + `WP_CLI_PHP_ARGS` prefix, JSON parsing, empty-output handling.

```python
class WPCli:
    def __init__(self, transport: SSHTransport, remote_timeout: int): ...
    def run(self, r: SiteReport, cmd: str, timeout: int | None = None) -> str: ...
    def text(self, r: SiteReport, cmd: str) -> str: ...
    def json(self, r: SiteReport, cmd: str, allow_empty: bool = False) -> list[dict]: ...
    def plugin_update_structured(self, r: SiteReport, slug: str) -> dict: ...
```

**Critical:** `_run_plugin_update_structured` contains the retry-on-empty and retry-on-no-entry logic. This moves verbatim into `WPCli.plugin_update_structured` with no logic changes.

**Testing unlock:** `SSHTransport.execute` is now injectable. Tests can subclass or mock it with a fake that returns canned stdout without spawning subprocess.

---

### Phase 4 — Extract inventory

**Files created:** `inventory/loader.py`

Move: `load_env`, `load_client_notes`, `_normalize_domain`, `skip_items_for_domain`, `resolve`, `_gather_client_files`, `_validate_app`, `_process_client_file`.

`_process_client_file` currently mixes loading, validation, dedupe checking, and sort ordering. After extraction it becomes a pure loader that returns `list[tuple[int, dict, SiteReport]]`. The dedupe check and staging-first sort move to `runner.py` where they belong conceptually.

```python
# inventory/loader.py
def load_env(path: Path) -> dict[str, str]: ...
def load_client_notes(client_path: Path) -> dict[str, Any]: ...
def skip_items_for_domain(notes: dict, domain: str) -> list[dict]: ...
def gather_client_files(args) -> list[Path]: ...
def load_client_file(path: Path, env: dict) -> list[SiteReport]:
    """Parse one *_cloudways.json, return validated SiteReports. 
    Raises InventoryError on fatal issues. Logs and skips individual 
    bad app blocks."""
```

---

### Phase 5 — Extract updater steps

**Files created:** `updater/steps.py`, `updater/rollback.py`, `updater/verify.py`, `updater/confidence.py`

This is the largest phase by line count but the lowest-risk by logic change — every `_step_*` becomes a module-level function that accepts `(r: SiteReport, wpcli: WPCli, args)` instead of `(self, r: SiteReport)`.

**`updater/verify.py`** — `_verify`, `_http_check`, `_http_check_one` plus `HTTP_RETRY_BACKOFFS`. The SSL context moves in here (constructed once, passed in or held by a `Verifier` class — either works). The HTTP retry logic is untouched.

**`updater/rollback.py`** — `_step_rollback` verbatim. The rollback shell script is a string constant; it must not be reformatted or the indentation changes will break the heredoc. Pull it to a named constant `ROLLBACK_SCRIPT_TEMPLATE` at the top of the file.

**`updater/confidence.py`** — `_compute_confidence` becomes a standalone function `compute_confidence(r: SiteReport) -> dict`. `CONFIDENCE_RULES` is imported from `core/constants.py`.

**`updater/steps.py`** — all remaining `_step_*` functions as module-level functions.

---

### Phase 6 — Slim down `WPUpdater`

After Phase 5, `WPUpdater` in `runner.py` becomes a thin orchestrator:

```python
class WPUpdater:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = ...
        self.env = load_env(args.env_file)
        self.log = make_logger(...)
        self.transport = SSHTransport(...)
        self.wpcli = WPCli(self.transport, ...)
        self.verifier = Verifier(ssl_ctx, args.http_timeout)
        self.reports: list[SiteReport] = []
        # circuit breaker state
        self._consecutive_execute_failures = 0
        self._run_abort_reason = ""
        # DB / dedupe
        self._db = _open_db(args)
        self._recent_successes = _load_recent_successes(args, self._db, self.log)

    def run(self) -> int:
        # same loop as today: gather → process per file → write summary
        ...

    def _process_site(self, r: SiteReport) -> None:
        # same step sequence and exception handling as today
        # calls steps.step_ssh_preflight(r, self.transport, ...) etc.
        ...
```

`_process_site` is the orchestration spine — it should remain one method in `runner.py` even after the steps are extracted. Do not turn it into a pipeline/chain of responsibility abstraction. The explicit sequential step calls with named `current_step` tracking is intentional and must survive.

---

### Phase 7 — Introduce `RunConfig` (optional, low priority)

The proliferation of `self.args.execute`, `self.args.remote_timeout`, `self.args.http_timeout` etc. passed through every call is the one real wart. A typed `RunConfig` dataclass built from `argparse.Namespace` in `main()` would clean this up:

```python
@dataclass(frozen=True)
class RunConfig:
    execute: bool
    remote_timeout: int
    http_timeout: int
    connect_timeout: int
    include_woocommerce: bool
    skip_staging: bool
    skip_ssl_verify: bool
    max_consecutive_failures: int
    skip_recent: int
    stream: bool
    log_dir: Path
    clients_dir: Path
    db_path: Path
    ssh_config: Path
    # ... etc
```

`build_cli()` still returns `argparse.Namespace`. `main()` converts it: `config = RunConfig(**vars(args))`. This is safe to defer — do it after Phase 6 when the shape of what gets passed around is stable.

---

## Migration Safety Rules

1. **One phase per PR.** Each phase must leave the test suite green and `python3 wp_update.py --help` working before merge.
2. **No logic changes in Phases 1–5.** If you find a bug during extraction, open a separate issue. Fix it after the refactor is merged.
3. **The rollback shell script is sacred.** Any whitespace change to the heredoc inside `_step_rollback` must be explicitly reviewed. Prefer a named constant so diffs are obvious.
4. **`SiteReport.to_dict()` is a contract.** The webui reads it; `db.py` ingests it. No field renames without a migration.
5. **`build_cli()` is a contract.** All existing CLI flags stay, in the same form. The refactor does not add or remove any flag.

---

## Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Import cycle between `core/models.py` and `ssh/transport.py` | Medium | `transport.py` imports from `core/models.py`, never the reverse |
| Rollback shell script whitespace change breaks remote execution | Low | Extract to `ROLLBACK_SCRIPT_TEMPLATE` constant; add a test that asserts the string contains `exit 99`, `exit 98`, `exit 97`, `exit 96` |
| `_run_plugin_update_structured` retry logic silently lost during extraction | Low | Copy verbatim; add unit test for each retry condition with a mocked `SSHTransport` |
| `SiteReport` credential fields exposed by accident in new code paths | Low | Add a test that calls `to_dict()` on a fully-populated report and asserts `ssh_password` and `master_password` are absent |
| `webui.py` subprocess call to `wp_update.py` breaks after restructure | None | Entry point (`wp_update.py`) and all CLI flags are unchanged |

---

## What You Get After All Phases

- **Testable SSH layer** — mock `SSHTransport.execute` to return canned output; test all step logic without a remote host
- **Testable inventory** — `load_client_file` takes a `Path`; pass a tempfile in tests
- **Testable confidence scoring** — `compute_confidence(r)` is a pure function
- **`WPUpdater` is ≤150 lines** — it only orchestrates; all decisions live in focused modules
- **`wp_update.py` entry point is ≤30 lines** — `build_cli()` + `WPUpdater(args).run()`
- **No behavioral change** — every design principle from the original docstring is preserved
