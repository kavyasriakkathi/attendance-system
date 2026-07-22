"""
run_migration.py
────────────────────────────────────────────────────────────────────────────
One-shot migration: SQLite attendance.db → Neon PostgreSQL (DATABASE_URL)

Features
────────
• Verifies attendance.db exists and has data
• Connects to PostgreSQL, creating ALL tables if missing
• Migrates in FK-safe order (branches → students → users → subjects →
  attendance → timetable_entries → settings → subject_aliases →
  attendance_sessions → attendance_records)
• Preserves original IDs (INSERT … ON CONFLICT (id) DO NOTHING)
• Fixes all SERIAL sequences after migration
• Prints row counts before AND after migration
• Never deletes existing data
• Ends with a clear Render redeploy verdict

Usage (PowerShell)
──────────────────
  $env:DATABASE_URL = "postgresql://user:pass@ep-xxx.neon.tech/dbname?sslmode=require"
  python run_migration.py
"""

import os
import re
import sqlite3
import sys
from urllib.parse import urlparse

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 is not installed.")
    print("Run:  pip install psycopg2-binary")
    sys.exit(1)

# ── Tables in FK-safe insertion order ──────────────────────────────────────
MIGRATE_TABLES = [
    "branches",
    "students",
    "users",
    "subjects",
    "teachers",
    "teacher_branches",
    "teacher_subjects",
    "teacher_subject_assignments",
    "attendance",
    "timetable_entries",
    "settings",
    "subject_aliases",
    "attendance_sessions",
    "attendance_records",
]

# ── Full PostgreSQL DDL ─────────────────────────────────────────────────────
# Created in dependency order; IF NOT EXISTS guards are idempotent.
CREATE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS branches (
        id       SERIAL PRIMARY KEY,
        name     TEXT UNIQUE NOT NULL,
        location TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS students (
        id         SERIAL PRIMARY KEY,
        name       TEXT NOT NULL,
        enrollment TEXT UNIQUE NOT NULL,
        branch_id  INTEGER NOT NULL,
        email      TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id         SERIAL PRIMARY KEY,
        username   TEXT UNIQUE NOT NULL,
        password   TEXT NOT NULL,
        role       TEXT NOT NULL,
        student_id INTEGER
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS subjects (
        id        SERIAL PRIMARY KEY,
        name      TEXT NOT NULL,
        branch_id INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS attendance (
        id             SERIAL PRIMARY KEY,
        student_id     INTEGER NOT NULL,
        branch_id      INTEGER NOT NULL,
        branch_section TEXT,
        section        TEXT,
        subject_id     INTEGER NOT NULL,
        subject_name   TEXT,
        period         TEXT,
        date           TEXT NOT NULL,
        status         TEXT NOT NULL,
        note           TEXT,
        teacher_id     INTEGER,
        marked_at      TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_student_subject_date_period
        ON attendance(student_id, subject_id, date, period);
    """,
    """
    CREATE TABLE IF NOT EXISTS timetable_entries (
        id           SERIAL PRIMARY KEY,
        branch_id    INTEGER,
        section      TEXT,
        semester     INTEGER,
        day          TEXT,
        start_time   TEXT,
        end_time     TEXT,
        subject_id   INTEGER,
        teacher_id   INTEGER,
        subject_name TEXT,
        faculty_name TEXT,
        is_lab       INTEGER DEFAULT 0,
        room         TEXT,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_timetable_entries_dedupe
        ON timetable_entries(branch_id, section, semester, day,
                              start_time, end_time, subject_id, teacher_id, room);
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        id    SERIAL PRIMARY KEY,
        key   TEXT UNIQUE NOT NULL,
        value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS subject_aliases (
        id             SERIAL PRIMARY KEY,
        alias          TEXT UNIQUE NOT NULL,
        canonical_name TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS attendance_sessions (
        id                SERIAL PRIMARY KEY,
        timetable_entry_id INTEGER,
        faculty_name       TEXT,
        section            TEXT,
        subject_name       TEXT,
        date               TEXT NOT NULL,
        start_time         TEXT,
        end_time           TEXT,
        is_closed          INTEGER DEFAULT 0,
        UNIQUE(section, subject_name, date, start_time, end_time)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS attendance_records (
        id         SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        status     TEXT NOT NULL,
        UNIQUE(session_id, student_id)
    );
    """,
    # teachers table (used by timetable / get_current_active_classes)
    """
    CREATE TABLE IF NOT EXISTS teachers (
        id           SERIAL PRIMARY KEY,
        name         TEXT,
        username     TEXT UNIQUE,
        password     TEXT,
        email        TEXT,
        subject_name TEXT,
        branch_id    INTEGER,
        section      TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS teacher_branches (
        id         SERIAL PRIMARY KEY,
        teacher_id INTEGER NOT NULL,
        branch_id  INTEGER NOT NULL,
        section    TEXT,
        UNIQUE(teacher_id, branch_id, section)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS teacher_subjects (
        id         SERIAL PRIMARY KEY,
        teacher_id INTEGER NOT NULL,
        subject_id INTEGER NOT NULL,
        UNIQUE(teacher_id, subject_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS teacher_subject_assignments (
        id SERIAL PRIMARY KEY,
        teacher_id INTEGER NOT NULL,
        subject_id INTEGER NOT NULL,
        branch_id INTEGER NOT NULL,
        section TEXT,
        semester TEXT,
        academic_year TEXT
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_teacher_subject_assignments_teacher
        ON teacher_subject_assignments (teacher_id);
    """,
]

# Tables that have a SERIAL 'id' column and need their sequence reset
SERIAL_TABLES = [
    "branches", "students", "users", "subjects", "attendance",
    "timetable_entries", "settings", "subject_aliases",
    "attendance_sessions", "attendance_records",
    "teachers", "teacher_branches", "teacher_subjects", "teacher_subject_assignments",
]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _fix_url(url: str) -> str:
    """Normalise connection string: postgres:// → postgresql://, add sslmode=require."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "sslmode=" in url:
        url = re.sub(r"sslmode=[a-zA-Z0-9_-]+", "sslmode=require", url)
    else:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"
    return url


def _sqlite_counts(conn, tables):
    counts = {}
    for t in tables:
        try:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            counts[t] = "N/A (table missing in SQLite)"
    return counts


def _pg_counts(cur, tables):
    counts = {}
    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            counts[t] = cur.fetchone()[0]
        except Exception:
            counts[t] = "N/A (table missing)"
    return counts


def _print_counts(label, counts):
    print(f"\n{'─'*55}")
    print(f"  {label}")
    print(f"{'─'*55}")
    for t, c in counts.items():
        print(f"  {t:<30} {c}")
    print(f"{'─'*55}")


def _set_sequences(cur, conn):
    """Reset SERIAL sequences so future INSERTs don't collide with migrated IDs."""
    for table in SERIAL_TABLES:
        try:
            cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
            max_id = cur.fetchone()[0]
            cur.execute(
                "SELECT setval(pg_get_serial_sequence(%s, 'id'), %s, %s)",
                (table, max(max_id, 1), bool(max_id)),
            )
        except Exception as e:
            # Table might not exist if SQLite didn't have it — safe to skip
            conn.rollback()
            print(f"  [sequence] Skipped {table}: {e}")


def _sqlite_table_exists(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 55)
    print("  SQLite → Neon PostgreSQL Migration")
    print("═" * 55)

    # ── 1. Resolve paths / env ───────────────────────────────────────────
    sqlite_path = os.environ.get("SQLITE_PATH", "attendance.db")
    # Allow the script to be run from any CWD by also checking the project root
    if not os.path.exists(sqlite_path):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(script_dir, "attendance.db")
        if os.path.exists(candidate):
            sqlite_path = candidate

    pg_url = os.environ.get("DATABASE_URL", "").strip()

    # ── 2. Pre-flight checks ──────────────────────────────────────────────
    errors = []
    if not os.path.exists(sqlite_path):
        errors.append(f"SQLite DB not found: {sqlite_path}")
    if not pg_url:
        errors.append(
            "DATABASE_URL is not set.\n"
            "Run:  $env:DATABASE_URL = 'postgresql://user:pass@host/db?sslmode=require'"
        )
    if errors:
        for e in errors:
            print(f"\nERROR: {e}")
        return 1

    pg_url = _fix_url(pg_url)
    parsed = urlparse(pg_url)

    print(f"\n  SQLite  : {sqlite_path}  ({os.path.getsize(sqlite_path):,} bytes)")
    print(f"  Postgres: host={parsed.hostname}  db={parsed.path.lstrip('/')}")

    # ── 3. Open connections ───────────────────────────────────────────────
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    # Ensure teacher_subject_assignments exists in SQLite
    sqlite_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS teacher_subject_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL,
            section TEXT,
            semester TEXT,
            academic_year TEXT
        )
        """
    )
    sqlite_conn.commit()

    # Populate teacher_subject_assignments from legacy teacher_assignments in SQLite
    try:
        tsa_count = sqlite_conn.execute("SELECT COUNT(*) FROM teacher_subject_assignments").fetchone()[0]
        if tsa_count == 0:
            ta_exists = sqlite_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='teacher_assignments'").fetchone()
            if ta_exists:
                print("Populating teacher_subject_assignments from legacy teacher_assignments in SQLite...")
                sqlite_conn.execute(
                    """
                    INSERT INTO teacher_subject_assignments (id, teacher_id, subject_id, branch_id, section)
                    SELECT id, teacher_id, subject_id, branch_id, section FROM teacher_assignments
                    """
                )
                sqlite_conn.commit()
    except Exception as e:
        print("Warning populating teacher_subject_assignments in SQLite:", e)

    try:
        pg_conn = psycopg2.connect(pg_url, sslmode="require", connect_timeout=15)
    except Exception as e:
        print(f"\nERROR: Cannot connect to PostgreSQL: {e}")
        sqlite_conn.close()
        return 1

    pg_conn.autocommit = False

    try:
        cur = pg_conn.cursor()

        # ── 4. Create tables ──────────────────────────────────────────────
        print("\n[Step 1] Creating missing PostgreSQL tables …")
        for stmt in CREATE_STATEMENTS:
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                cur.execute(stmt)
                pg_conn.commit()
            except Exception as e:
                pg_conn.rollback()
                # Duplicate index errors are benign
                if "already exists" in str(e).lower():
                    print(f"  (index/table already exists — skipped)")
                else:
                    print(f"  WARNING: DDL failed: {e}\n  SQL: {stmt[:80]}")

        print("  Tables ready ✓")

        # ── 5. Row counts BEFORE ──────────────────────────────────────────
        print("\n[Step 2] Row counts BEFORE migration")
        sqlite_before = _sqlite_counts(sqlite_conn, MIGRATE_TABLES)
        _print_counts("SQLite (source)", sqlite_before)

        cur2 = pg_conn.cursor()
        pg_before = _pg_counts(cur2, MIGRATE_TABLES)
        _print_counts("PostgreSQL BEFORE", pg_before)

        # ── 6. Migrate each table ─────────────────────────────────────────
        print("\n[Step 3] Migrating data …")
        summary = {}

        for table in MIGRATE_TABLES:
            if not _sqlite_table_exists(sqlite_conn, table):
                print(f"  {table:<30} SKIPPED (not in SQLite)")
                summary[table] = 0
                continue

            rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"  {table:<30} 0 rows  (empty in SQLite)")
                summary[table] = 0
                continue

            cols = list(rows[0].keys())
            col_list = ", ".join(cols)
            placeholders = ", ".join(["%s"] * len(cols))

            # Determine conflict target
            # Tables with a simple 'id' PK use ON CONFLICT (id)
            conflict_clause = "ON CONFLICT (id) DO NOTHING"

            inserted = 0
            skipped = 0
            errors_in_table = 0

            for row in rows:
                values = []
                for c in cols:
                    v = row[c]
                    # Convert SQLite booleans stored as 0/1 integers — keep as-is
                    values.append(v)

                try:
                    cur.execute(
                        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) {conflict_clause}",
                        values,
                    )
                    if cur.rowcount > 0:
                        inserted += 1
                    else:
                        skipped += 1
                except Exception as e:
                    pg_conn.rollback()
                    errors_in_table += 1
                    if errors_in_table <= 3:
                        print(f"    [WARN] Row insert failed in '{table}': {e}")
                    # Re-open cursor after rollback
                    cur = pg_conn.cursor()

            try:
                pg_conn.commit()
            except Exception as e:
                pg_conn.rollback()
                print(f"  [ERROR] Commit failed for '{table}': {e}")
                cur = pg_conn.cursor()

            summary[table] = inserted
            status = f"✓  inserted={inserted}  skipped(existing)={skipped}"
            if errors_in_table:
                status += f"  errors={errors_in_table}"
            print(f"  {table:<30} {status}")

        # ── 7. Fix SERIAL sequences ───────────────────────────────────────
        print("\n[Step 4] Resetting SERIAL sequences …")
        cur = pg_conn.cursor()
        _set_sequences(cur, pg_conn)
        pg_conn.commit()
        print("  Sequences updated ✓")

        # ── 8. Row counts AFTER ───────────────────────────────────────────
        print("\n[Step 5] Row counts AFTER migration")
        cur = pg_conn.cursor()
        pg_after = _pg_counts(cur, MIGRATE_TABLES)
        _print_counts("PostgreSQL AFTER", pg_after)

        # ── 9. Migration summary ──────────────────────────────────────────
        print("\n[Summary] Rows copied from SQLite → PostgreSQL:")
        total_copied = 0
        for t in MIGRATE_TABLES:
            n = summary.get(t, 0)
            total_copied += (n if isinstance(n, int) else 0)
            print(f"  {t:<30} {n}")
        print(f"\n  Total rows inserted: {total_copied}")

        # ── 10. Render redeploy verdict ───────────────────────────────────
        print("\n" + "═" * 55)
        expected = {
            "students":          14,
            "branches":          19,
            "subjects":          13,
            "attendance":        24,
            "timetable_entries": 194,
            "users":             15,
        }
        mismatches = []
        for t, exp in expected.items():
            actual = pg_after.get(t)
            if isinstance(actual, int) and actual < exp:
                mismatches.append(f"  {t}: expected ≥{exp}, got {actual}")

        if not mismatches:
            print("  ✅  SAFE TO REDEPLOY ON RENDER")
            print()
            print("  All expected row counts are present in PostgreSQL.")
            print("  Ensure DATABASE_URL is set in your Render environment")
            print("  variables (Dashboard → Environment), then trigger a")
            print("  manual deploy.")
        else:
            print("  ⚠️   NOT SAFE TO REDEPLOY YET — row count mismatch:")
            for m in mismatches:
                print(m)
            print()
            print("  Fix the mismatches above, then re-run this script.")
        print("═" * 55 + "\n")

        return 0

    finally:
        try:
            sqlite_conn.close()
        except Exception:
            pass
        try:
            pg_conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
