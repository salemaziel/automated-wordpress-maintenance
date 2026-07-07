Good. Full picture. Here's the analysis:

***

## `wp profile` Analysis for This Project

The fundamental difference from `wp doctor` is **purpose**: doctor tells you *what is wrong*, profile tells you *what is slow* . It instruments WordPress's PHP execution process in real-time — bootstrap, main_query, template stages — and reports wall time, cache hit ratio, query count, and HTTP request count per hook and per callback . This is performance telemetry, not health assessment.

That distinction matters for how it fits (or doesn't) into your maintenance automation project.

***

## What `wp profile` Actually Measures

Four subcommands, each returning structured JSON :

| Command | What it measures | Key fields |
|---|---|---|
| `wp profile stage` | Bootstrap / main_query / template wall time | `time`, `cache_ratio`, `query_count`, `request_count` |
| `wp profile stage bootstrap` | Per-hook timing during bootstrap | `hook`, `time`, `cache_ratio` |
| `wp profile hook` | All hook callbacks across a full request | `callback`, `time`, `cache_hits`, `cache_misses` |
| `wp profile queries` | Every DB query during a request | `query`, `time`, `caller` |

The `--spotlight` flag filters out near-zero entries to surface only the meaningful slowness . `--format=json` makes all of it machine-readable.

***

## The Core Tension With This Project

Your project is a **mutation tool** — it updates things. `wp profile` is a **read-only diagnostic tool** — it instruments a live page load. The question is whether performance regression detection belongs inside an update automation loop, and the honest answer is: **partially**.

The use cases split into two distinct categories:

### Category A: Genuinely useful, low overhead

**Pre/post-update performance delta on `stage` metrics.** Running `wp profile stage --format=json` before and after a plugin batch update takes one extra SSH call each way and gives you hard numbers on whether `bootstrap` time degraded. The stage-level snapshot is coarse but fast — it simulates one page load and returns three numbers. This is the same cost as your existing `_http_check` and provides performance regression data your HTTP check cannot see.

```python
# updater/verify.py

@dataclass
class ProfileStageSnapshot:
    stage: str
    time_s: float
    cache_ratio: float
    query_count: int

def snapshot_profile_stages(wpcli: WPCli, r: SiteReport) -> list[ProfileStageSnapshot]:
    """wp profile stage --all --format=json. Returns [] if not installed."""
    try:
        raw = wpcli.json(r, "profile stage --all --format=json", allow_empty=True)
    except WPCliError:
        return []
    results = []
    for row in raw:
        if row.get("stage") == "total":
            continue
        with contextlib.suppress(KeyError, ValueError):
            results.append(ProfileStageSnapshot(
                stage=row["stage"],
                time_s=float(str(row.get("time", "0")).rstrip("s")),
                cache_ratio=float(str(row.get("cache_ratio", "0")).rstrip("%")),
                query_count=int(row.get("query_count", 0)),
            ))
    return results
```

Store pre-update snapshots in `SiteReport.baseline["profile_stages"]`, compare after the full update batch, log any stage that regressed by more than a configurable threshold (e.g. `>25% slower`). This is **not a rollback trigger** — performance regressions are ambiguous (could be server load, caching state, cold start). Log and flag for human review.

### Category B: Useful for investigation, not for automation

**`wp profile hook --all`** and **`wp profile queries`** are deep-drill tools. They take several seconds to run, produce hundreds of rows of callback-level data, and require a human to interpret . Running these in an automated update loop — especially across multiple client sites — would:

1. Add significant per-site runtime (5–20 seconds per site for hook profiling)
2. Generate large JSON blobs that bloat the summary output
3. Produce data that's only actionable in interactive investigation

These belong in the **webui**, not in the update runner. A "Profile Site" button per client in the maintenance console that triggers an on-demand `wp profile stage bootstrap --spotlight --format=json` and renders it as a flame chart or table is a genuinely useful feature. This is where the heavy profiling commands pay off — triggered by a human looking at a slow site, not run blindly on every update.

***

## Concrete Integration Points

### In `wp_update.py` / refactored `updater/verify.py`

Only `wp profile stage --all --format=json` belongs here. The integration is:

```
pre-flight
  → snapshot_profile_stages()  → stored in baseline["profile_stages"]

post-update (after full plugin/theme/core batch)
  → snapshot_profile_stages()  → compare to baseline
  → if any stage >PROFILE_REGRESSION_THRESHOLD% slower:
      log WARNING with delta table
      add to SiteReport as a new field: performance_regression: bool
      (never trigger rollback on perf data alone)
```

The `PROFILE_REGRESSION_THRESHOLD` constant (default `30`) goes in `core/constants.py` alongside `CONFIDENCE_RULES`. The `performance_regression` flag surfaces in the webui summary and the JSON report so the human operator can decide whether to investigate.

### In the webui (future, separate feature)

An on-demand profile panel per site:
- Calls `wp profile stage --all --spotlight --format=json` → renders as a stage timing table
- Calls `wp profile stage bootstrap --spotlight --format=json` → renders top slow hooks
- Calls `wp profile queries --time_threshold=0.05 --format=json` → renders slow queries

This is a natural extension of the existing run-and-stream pattern in `webui.py`. The SSE streaming infrastructure you already have handles the output perfectly — profile commands emit progressive output that users expect to watch unfold.

***

## Installation: Same Problem as Doctor, Same Solution

`wp profile` is a community package requiring `wp package install wp-cli/profile-command:@stable` . It requires WP-CLI v2.13+. Use the same graceful-degradation pattern as doctor — detect absence via `WPCliError` on the first call, log a warning, skip silently. Add it to the same optional installation check path.

***

## Honest Assessment vs. `wp doctor`

| Dimension | `wp doctor` | `wp profile` |
|---|---|---|
| Value in automation loop | High — blocking checks, confidence scoring, post-update verify | Low-medium — stage snapshot only; rest is interactive |
| Value in webui | Medium — could show health dashboard | High — on-demand profiling panel is genuinely useful |
| Runtime cost per site | Low (~1–2s for `check --all`) | Low for stage, High for hook/queries |
| Output actionability | Immediate — pass/fail per check | Requires human interpretation |
| Rollback signal | Yes (`core-verify-checksums`, `php-in-upload` errors) | No — never a rollback trigger |
| Priority | Implement first | Implement stage delta in runner; rest deferred to webui |

`wp profile` is the better tool for the webui and for on-demand investigation. `wp doctor` is the better tool for the automated update loop. They're complementary, not competing — but if you can only do one in the runner, doctor wins on every dimension.

The most disciplined integration: add `snapshot_profile_stages()` to the update runner as a passive baseline/compare (no rollback, just delta logging), and build the full profile UI as a webui feature once the FastAPI port is done and you have a proper async handler to stream the output to the browser.
