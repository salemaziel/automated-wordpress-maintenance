Good — I have what I need. Here's the full analysis:

***

## How `wp doctor` Fits This Project

`wp doctor` runs a battery of named, configurable health checks against a WordPress install and returns structured JSON — status (`success`/`warning`/`error`) + message per check . The checks cover things your project currently has no visibility into at all: autoloaded options bloat, eval+base64 obfuscation patterns in files, duplicate cron events, `WP_DEBUG` left on in production, `blog_public` accidentally set to 0, PHP files in the upload directory . These are exactly the class of silent problems that survive a successful plugin update run and cause client calls two weeks later.

The key design point: `wp doctor check --all --format=json` returns machine-readable output , which means your existing `WPCli.json()` abstraction can consume it with zero new SSH infrastructure.

***

## Where It Lands in the Current Architecture

### Pre-flight: replaces / augments your confidence score

Right now `_compute_confidence` infers site health indirectly — plugin update count, disk tightness, WooCommerce presence, PHP version — none of which actually inspect the WordPress application state . Doctor runs against the live install and returns ground-truth application health. A natural pattern:

```
ssh preflight
  → wp doctor check --all --format=json          ← NEW
  → parse into DoctorResult (warning/error counts)
  → fold into confidence score (errors = hard block, warnings = penalty)
  → baseline collection (existing)
  → backup
  → update steps...
```

An `error`-severity doctor result (e.g. `core-verify-checksums` failing, or `php-in-upload` finding a webshell) should **block execution** before touching anything. A `warning` lowers confidence. This is strictly better than the current heuristic scoring.

### Post-update: replaces your HTTP health check for application-layer checks

Currently `_verify` does an HTTP GET and scans for `FATAL_MARKERS` in the response body . That catches PHP fatals but misses: cron queue explosion triggered by an update, a plugin enabling `SAVEQUERIES` in production, or autoloaded options ballooning after a plugin update. Running `wp doctor` post-update catches all of these. The pattern becomes:

```
update plugin X
  → HTTP check (fast, catches hard crashes)
  → wp doctor check cron-count,cron-duplicates,autoload-options-size --format=json (catches soft regressions)
  → if any ERROR: trigger rollback
  → if any new WARNING vs pre-flight baseline: log, continue
```

The "new WARNING vs baseline" delta is the important nuance — you'd store the pre-flight doctor snapshot in `SiteReport.baseline` (it already has a `baseline` dict field ) and compare after each plugin update, so you catch regressions introduced by specific plugins.

***

## What This Changes in the Refactor Plan

The refactor plan already calls for `updater/verify.py` as a standalone module . Doctor fits cleanly there:

```python
# updater/verify.py additions

@dataclass
class DoctorResult:
    check: str
    status: str          # success | warning | error
    message: str

def run_doctor(wpcli: WPCli, r: SiteReport, checks: list[str] | None = None) -> list[DoctorResult]:
    """Run wp doctor check. Returns [] if doctor is not installed (graceful degradation)."""
    cmd = "doctor check --all --format=json"
    if checks:
        cmd = f"doctor check {','.join(checks)} --format=json"
    try:
        raw = wpcli.json(r, cmd, allow_empty=True)
    except WPCliError:
        return []   # doctor not installed — degrade gracefully
    return [DoctorResult(**item) for item in raw]

def doctor_blocks_execution(results: list[DoctorResult]) -> str:
    """Return a reason string if any result should block an update run, else ''."""
    blockers = [r for r in results if r.status == "error" and r.check in DOCTOR_HARD_BLOCKS]
    if blockers:
        return "; ".join(f"{r.check}: {r.message}" for r in blockers)
    return ""
```

`DOCTOR_HARD_BLOCKS` would be a constant in `core/constants.py` — the set of checks that are hard execution blockers vs. just warnings. A reasonable default:

```python
DOCTOR_HARD_BLOCKS = frozenset({
    "core-verify-checksums",   # core files tampered/corrupted
    "php-in-upload",           # probable webshell — do not update, alert
    "file-eval",               # obfuscated eval in files — same
})
```

Everything else degrades to a confidence penalty rather than a hard block.

***

## The Installation Problem

`wp doctor` is a [community package](https://github.com/wp-cli/doctor-command), not bundled with WP-CLI . It requires `wp package install wp-cli/doctor-command` on each remote host — you can't assume it's present. Three approaches:

| Approach | Pros | Cons |
|---|---|---|
| Install as part of SSH preflight if missing | Fully automatic, transparent | Writes to remote home dir; needs network from host |
| Gate with `wp package list --format=json` check; skip doctor if absent | Zero surprise | Silent degradation is easy to forget |
| Add doctor install to client onboarding scripts | One-time, clean | Manual per-host step |

The safest default is **option 2** — detect and degrade gracefully (the `try/except WPCliError` in the snippet above handles the `command not found` case). Add a log warning when doctor is missing so it surfaces in the run report. Document option 1 as an opt-in flag (`--install-doctor`).

***

## Custom `doctor.yml` Per Client

Doctor's check config is a YAML file . You could ship a project-level `doctor.yml` alongside your client files and `--config-file` it during the remote call — this is the right place for per-client thresholds (e.g. a media-heavy site gets a higher `autoload-options-size` warning threshold). This fits naturally into the `notes.json` pattern you already have: add a `doctor_config` section per domain and render it to a tempfile before the SSH call.

***

## Net Assessment

Doctor doesn't change the fundamental architecture — it slots into pre-flight and post-update verify as an **additional data source**. The payoff is real: you go from inferring site health to measuring it. The confidence score becomes honest. Post-update verification catches application-layer regressions, not just hard PHP crashes. And the `core-verify-checksums` / `php-in-upload` checks add a lightweight security gate that currently doesn't exist at all.

The only genuine risk is the installation dependency. Build in graceful degradation from day one and it's a pure improvement.
