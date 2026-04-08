"""Merge newer rows from corrupted jobs.db into a recovered backup copy.

Committed as permanent tooling — 3 corruption incidents in 2 days means
this is a known failure mode with a known recovery path.

Usage:
    uv run python scripts/recover_db.py              # full recovery
    uv run python scripts/recover_db.py --dry-run    # report what would be merged
    uv run python scripts/recover_db.py --verify-only # verify existing recovered DB

Prerequisites:
    1. Stop all processes (web server, workers)
    2. cp jobs.db.backup jobs.db.recovered
    3. Run this script
    4. mv jobs.db.recovered jobs.db
    5. rm -f jobs.db-wal jobs.db-shm
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

CORRUPTED = Path("jobs.db")
RECOVERED = Path("jobs.db.recovered")


def recover(dry_run: bool = False) -> None:
    """Merge newer rows from corrupted DB into recovered backup copy."""
    if not CORRUPTED.exists():
        raise FileNotFoundError(f"Corrupted DB not found: {CORRUPTED}")
    if not RECOVERED.exists():
        raise FileNotFoundError(f"Recovered DB not found: {RECOVERED}\nRun: cp jobs.db.backup jobs.db.recovered")

    # Open corrupted DB read-only to prevent accidental writes
    with (
        closing(sqlite3.connect(f"file:{CORRUPTED}?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(str(RECOVERED))) as dst,
    ):
        src.row_factory = sqlite3.Row
        # Defensive PRAGMAs for reading corrupted DB — catch page-level
        # corruption early rather than letting it propagate through results
        src.execute("PRAGMA cell_size_check=ON")
        src.execute("PRAGMA mmap_size=0")  # prevent segfaults from corrupted pages
        dst.execute("PRAGMA journal_mode=WAL")
        dst.execute("PRAGMA foreign_keys=OFF")  # disable during merge

        # CRITICAL: Drop the updated_at trigger — it fires on UPDATE and
        # overwrites the historical updated_at we're trying to preserve
        dst.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")

        # --- Merge new rows for AUTOINCREMENT tables ---
        # Table names are hardcoded literals — not user input, f-string is safe.
        for table in ("jobs", "provider_runs", "job_phase_durations"):
            max_id = dst.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 0
            try:
                rows = src.execute(f"SELECT * FROM {table} WHERE id > ?", (max_id,)).fetchall()
            except sqlite3.DatabaseError as e:
                print(f"WARNING: {table} unreadable from corrupted source: {e}")
                continue
            if rows:
                # Get column names from the rows themselves (safe) — NOT from
                # a second query to the corrupted source
                cols = rows[0].keys()
                placeholders = ",".join(["?"] * len(cols))
                if dry_run:
                    print(f"{table}: would merge {len(rows)} newer rows (id > {max_id})")
                else:
                    dst.executemany(
                        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                        rows,
                    )
                    print(f"{table}: merged {len(rows)} newer rows")

        # --- Upsert jobs with newer updated_at (data integrity fix) ---
        # MAX(id) merge only captures new rows. Existing jobs may have had
        # status/updated_at changes since backup. Update those too.
        try:
            # Get column names from dst (healthy, guaranteed not to fail)
            cols = [d[0] for d in dst.execute("SELECT * FROM jobs LIMIT 0").description]
            backup_max_id = dst.execute("SELECT MAX(id) FROM jobs").fetchone()[0] or 0
            updated = 0
            for row in src.execute("SELECT * FROM jobs WHERE id <= ?", (backup_max_id,)):
                row_dict = dict(row)
                backup_updated = dst.execute("SELECT updated_at FROM jobs WHERE id = ?", (row_dict["id"],)).fetchone()
                if backup_updated and row_dict["updated_at"] > backup_updated[0]:
                    set_clause = ", ".join(f"{c} = ?" for c in cols if c != "id")
                    vals = [row_dict[c] for c in cols if c != "id"] + [row_dict["id"]]
                    if not dry_run:
                        dst.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", vals)
                    updated += 1
            if updated:
                action = "would update" if dry_run else "updated"
                print(f"jobs: {action} {updated} existing rows with newer data")
        except sqlite3.DatabaseError as e:
            print(f"WARNING: could not upsert existing jobs: {e}")

        # job_metrics uses job_id as PK (not auto-increment) — backup has MORE rows.
        # Keep backup's version (superset).

        if dry_run:
            print("\nDry run complete. No changes written.")
            return

        # sqlite_sequence: use max IDs from the LIVE (corrupted) DB, not the
        # recovered DB. Delta rows in corrupted tables may be unreadable, so
        # MAX(id) in recovered DB could be lower than the live DB's max.
        # Using live max prevents autoincrement ID collisions on new inserts.
        for table in ("jobs", "provider_runs", "job_phase_durations"):
            try:
                live_max = src.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 0
            except sqlite3.DatabaseError:
                live_max = 0
            recovered_max = dst.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 0
            seq = max(live_max, recovered_max)
            dst.execute(
                "INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                (table, seq),
            )
            print(f"sqlite_sequence[{table}] = {seq}")

        # Recreate the trigger before committing
        dst.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_jobs_updated_at AFTER UPDATE ON jobs
            BEGIN
                UPDATE jobs SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
            END
        """)

        dst.execute("PRAGMA foreign_keys=ON")
        dst.commit()

        # Verify
        verify(dst)


def verify(conn: sqlite3.Connection | None = None) -> None:
    """Run all post-recovery verification checks."""
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(str(RECOVERED))

    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Recovery failed integrity check: {result}")
        print(f"integrity_check: {result}")

        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key violations found: {fk_errors[:5]}")
        print("foreign_key_check: ok")

        # Orphan checks for child tables
        for child, fk_col, parent in [
            ("provider_runs", "job_id", "jobs"),
            ("job_phase_durations", "job_id", "jobs"),
            ("job_metrics", "job_id", "jobs"),
            ("events", "job_id", "jobs"),
        ]:
            orphans = conn.execute(
                f"SELECT COUNT(*) FROM {child} WHERE {fk_col} NOT IN (SELECT id FROM {parent})"
            ).fetchone()[0]
            if orphans:
                print(f"WARNING: {orphans} orphaned rows in {child}")

        # Row counts report
        print("\nRow counts:")
        for table in ("jobs", "events", "provider_runs", "job_phase_durations", "job_metrics"):
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {count} rows")
        print("\nRecovery verification successful.")
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Recover corrupted jobs.db from backup")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be merged without writing",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Run verification checks on existing recovered DB",
    )
    args = parser.parse_args()

    if args.verify_only:
        verify()
    else:
        recover(dry_run=args.dry_run)
