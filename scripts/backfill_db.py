#!/usr/bin/env python3
"""Backfill the SQLite history DB from existing wp-update-summary-*.json files.

Idempotent: re-running re-ingests the same summaries (each one is keyed by
its synthetic webui_run_id = "cli-<run_id>"). Safe to run alongside the
webui because writes are short and serialized through SQLite WAL.

Usage:
    .venv/bin/python scripts/backfill_db.py
    .venv/bin/python scripts/backfill_db.py --logs-dir logs --db-path db/wpmaint.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db as _db  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--logs-dir", type=Path, default=ROOT / "logs")
    p.add_argument("--db-path", type=Path, default=ROOT / "db" / "wpmaint.db")
    args = p.parse_args()

    args.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _db.open_db(args.db_path)

    summaries = sorted(args.logs_dir.glob("wp-update-summary-*.json"))
    if not summaries:
        print(f"No summaries found in {args.logs_dir}")
        return 0

    ok = skipped = failed = 0
    for path in summaries:
        try:
            webui_run_id = _db.ingest_cli_summary(
                conn, summary_path=path, started_by="cli-backfill"
            )
        except Exception as exc:
            print(f"FAIL  {path.name}: {exc}", file=sys.stderr)
            failed += 1
            continue
        if webui_run_id is None:
            print(f"SKIP  {path.name} (unparseable / no run_id)")
            skipped += 1
        else:
            ok += 1
    print(f"\nIngested: {ok}   Skipped: {skipped}   Failed: {failed}")
    print(f"DB: {args.db_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
