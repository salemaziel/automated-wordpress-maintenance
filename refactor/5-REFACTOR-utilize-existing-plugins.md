# Utilizing already-installed plugins

**NOTE: Ideas mentioned in this file will modify and cause changes in the refactor plan. Explicit mention of the changes at the bottom of this document.**

***

## The Detection-First Architecture

Before anything else, your preflight needs a **capability inventory** — what plugins are installed, active, and have usable WP-CLI or REST integrations. This becomes a new preflight step that runs before any backup or health check decision:

```python
@dataclass
class SiteCapabilities:
    # Backup
    backup_plugin: str | None        # "updraftplus", "backwpup", "duplicator-pro", etc.
    backup_cli: bool                 # wp <plugin> backup command available
    backup_remote_configured: bool   # has remote storage destination set up
    
    # Health
    site_health_available: bool      # WP core >= 5.2, REST accessible
    
    # Caching
    object_cache_plugin: str | None  # "redis-cache", "w3-total-cache", etc.
    cache_flush_cli: bool            # wp cache flush works
    
    # Security
    security_plugin: str | None      # "wordfence", "ithemes-security", etc.
    maintenance_mode_available: bool # can we put site in maintenance mode cleanly
```

This object gets built during preflight and drives every downstream decision. No capability → fall back to current WP-CLI behavior. Capability present → use it.

***

## Plugin-by-Plugin Breakdown

### UpdraftPlus

The most widely deployed backup plugin across client sites. Already covered in depth, but the full picture:

**WP-CLI surface:**
```bash
wp updraftplus backup --include-db --include-plugins --include-themes --include-uploads --include-others
wp updraftplus restore <backup-id>
wp updraftplus get-backups --format=json     # list existing backups with IDs and timestamps
```

**What you gain:** Chunked backup that handles large sites, remote storage push (S3/GDrive/Dropbox if configured), backup ID you can reference for restore. The `get-backups` call lets you verify a backup completed and get its ID before proceeding with updates — something your current `tar` approach can't confirm atomically.

**The remote storage question:** Run `wp updraftplus get-backups --format=json` — if the most recent backup has a `service` field that isn't `local`, remote storage is configured. If it's all local, you're back to the same problem as your `tar` — the backup lives on the same filesystem you might destroy. In that case: run the UpdraftPlus backup for the client's records and benefit, but still keep your own `tar` as the actual rollback artifact.

**One genuine win regardless of remote storage:** UpdraftPlus backup metadata (timestamp, size, components) goes into `SiteReport.baseline["backup"]`, which currently has nothing. The client-facing summary can now say "backed up via UpdraftPlus to S3 before updates" instead of silently relying on your internal tar.

***

### WooCommerce

Already in your code as a skip-and-flag condition , but there's more WP-CLI surface you're not using:

```bash
wp wc tool run clear_transients          # clear stale WC transients pre-update
wp wc update                             # run DB migrations post core/WC update  
wp wc system-status --format=json        # WC system status (PHP version, DB, extensions)
```

The `system-status` call is the interesting one — it returns WooCommerce's own health report including active payment gateways, DB version, and known incompatibilities. On sites where `has_woocommerce: true`, running this pre/post update gives you a WC-aware health check that your current HTTP check completely misses. A WC site that loads fine over HTTP but has broken payment gateways after an update is a nightmare — this catches it.

***

### Wordfence / iThemes Security / Solid Security

Security plugins are the **observability problem** mentioned earlier — they log plugin activations, file changes, and REST calls. But they also have WP-CLI surfaces you can use to your advantage:

```bash
wp wordfence scan --type=malware          # run a malware scan
wp wordfence whitelist-url <url>          # suppress alerts for known-good changes
```

More practically: if Wordfence is active, your file changes during update (the `public_html` tar restore during rollback especially) will trigger a file change alert to the site owner. You can't suppress that entirely, but you can **pre-notify** by noting in the run report that Wordfence alerts may fire. The `security_plugin` field in `SiteCapabilities` lets you add this to the post-run summary automatically.

***

### Caching Plugins (W3 Total Cache, WP Super Cache, LiteSpeed Cache, WP Rocket)

This is one of the highest-value integrations and currently completely absent from your script. After every update, stale cache can make a site look broken when it's actually fine — and can make a broken site look fine. Cache flush should happen **after every update step**, not just at the end.

```bash
wp w3-total-cache flush all
wp super-cache flush
wp litespeed-purge all
wp rocket clean                    # WP Rocket WP-CLI
wp cache flush                     # WP core object cache (always available)
```

Your script already runs `wp cache flush` (WP core object cache) but doesn't touch page caches. Detection pattern:

```python
CACHE_FLUSH_COMMANDS = {
    "w3-total-cache":       "w3-total-cache flush all",
    "wp-super-cache":       "super-cache flush", 
    "litespeed-cache":      "litespeed-purge all",
    "wp-rocket":            "rocket clean",
    "wp-fastest-cache":     "cache flush",     # uses core command
}
```

Build the capability map at preflight by checking `wp plugin list --status=active --format=json` once, then map slugs to flush commands. After each plugin update, run the appropriate flush command. This is probably **the single biggest reliability improvement** available from existing-plugin leverage — it directly prevents the false-positive health check failures (site serves stale error page from cache, HTTP check fails, rollback triggered, site was actually fine).

***

### Maintenance Mode Plugins

Some clients have Coming Soon / Maintenance Mode plugins (SeedProd, WP Maintenance Mode, etc.). If active, your update run should coordinate with them:

```bash
wp maintenance-mode activate     # WP core maintenance mode (always available)
# or plugin-specific equivalents
```

WP core's built-in maintenance mode (`wp maintenance-mode activate`) creates `.maintenance` in `ABSPATH` — this is already in WP-CLI core, no plugin required. The point is that you're currently **not using it at all**. Putting the site in maintenance mode before updates, then deactivating after, prevents visitors from hitting a half-updated state. Your `FATAL_MARKERS` HTTP check currently runs against the live site — during maintenance mode that check would hit the maintenance page instead. Coordinate accordingly: disable HTTP checks during maintenance window, re-enable after deactivation.

***

## Revised Preflight Sequence

With all of this, the preflight step order becomes:

```
1. SSH preflight (auth cascade) — existing
2. wp plugin list --status=active --format=json → build SiteCapabilities
3. Determine backup strategy:
     UpdraftPlus + CLI available → wp updraftplus backup
     UpdraftPlus + no CLI → tar fallback  
     other known plugin → log, tar fallback
     nothing → tar
4. Verify backup completion (get-backups for UpdraftPlus, file existence for tar)
5. wp maintenance-mode activate  ← NEW
6. Flush caches (using capability-mapped command) ← NEW
7. wp site-health → if REST accessible (or via wp doctor if installed)
8. WC system-status if has_woocommerce ← NEW
9. Collect baseline (existing)
→ proceed to updates
```

And post-update, before health check:

```
10. Flush caches again ← CRITICAL
11. wp maintenance-mode deactivate
12. HTTP health check (now against live, post-cache-flush state)
13. WC system-status comparison if has_woocommerce
14. Profile stage snapshot if wp profile installed
```

***

## What This Changes in the Refactor Plan

The `SiteCapabilities` detection belongs in `updater/steps.py` as `step_detect_capabilities()` — it runs once per site, early in preflight, and its result gets stored on `SiteReport` (add a `capabilities: SiteCapabilities` field). Every subsequent step checks `r.capabilities` instead of re-detecting. The backup strategy selection, cache flush command selection, and WC-specific steps all branch off that single capabilities object. Nothing about the SSH transport or WP-CLI abstraction layers changes — this is pure orchestration logic on top of what already exists.
