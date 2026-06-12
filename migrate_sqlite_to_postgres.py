import os
import sqlite3
from urllib.parse import urlparse

import psycopg2


TABLES = [
    "branches",
    "students",
    "subjects",
    "attendance",
    "timetable_entries",
    "users",
    "settings",
]


def _ensure_sslmode(url: str) -> str:
    """Ensure url contains sslmode=require and no other sslmode settings."""
    if "sslmode=" in url:
        url = url.replace("sslmode=prefer", "sslmode=require")
        url = url.replace("sslmode=disable", "sslmode=require")
    if "sslmode=require" not in url:
        if "sslmode=" in url:
            import re as _re
            url = _re.sub(r"sslmode=[a-zA-Z0-9_-]+", "sslmode=require", url)
        else:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
    return url


def _set_sequence(conn, table: str, id_col: str = "id") -> None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COALESCE(MAX({id_col}), 0) FROM {table}")
        max_id = int(cur.fetchone()[0])
        cur.execute(
            "SELECT setval(pg_get_serial_sequence(%s, %s), %s, %s)",
            (table, id_col, max_id, bool(max_id)),
        )


def _print_row_counts_sqlite(conn, tables):
    print("\nSQLite row counts before migration:")
    for table in tables:
        try:
            count = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[0]
            print(f"Table: {table} | Rows: {count}")
        except Exception as exc:
            print(f"Table not found: {table}")


def _print_row_counts_postgres(conn, tables, label="Postgres row counts"):
    print(f"\n{label}:")
    with conn.cursor() as cur:
        for table in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                print(f"Table: {table} | Rows: {count}")
            except Exception as exc:
                print(f"Table not found: {table}")


def main() -> int:
    sqlite_path = os.environ.get("SQLITE_PATH", "attendance.db")
    pg_url = os.environ.get("DATABASE_URL")

    if not pg_url:
        print("ERROR: DATABASE_URL is not set.")
        print("Set it to your Neon PostgreSQL connection string, e.g.:")
        print("  $env:DATABASE_URL='postgresql://user:pass@ep-xxx.neon.tech/attendance?sslmode=require'")
        return 2

    if pg_url.startswith("postgres://"):
        pg_url = pg_url.replace("postgres://", "postgresql://", 1)

    pg_url = _ensure_sslmode(pg_url)

    if not os.path.exists(sqlite_path):
        print(f"ERROR: SQLite DB not found at {sqlite_path}")
        return 2

    print(f"SQLite: {sqlite_path}")
    parsed = urlparse(pg_url)
    print(f"Postgres: host={parsed.hostname} port={parsed.port} db={parsed.path.lstrip('/')}")

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg2.connect(pg_url, sslmode="require", connect_timeout=10)

    try:
        with pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS timetable_entries (
                        id SERIAL PRIMARY KEY,
                        branch_id INTEGER,
                        section TEXT,
                        semester INTEGER,
                        day TEXT,
                        start_time TEXT,
                        end_time TEXT,
                        subject_id INTEGER,
                        teacher_id INTEGER,
                        subject_name TEXT,
                        faculty_name TEXT,
                        is_lab INTEGER DEFAULT 0,
                        room TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_timetable_entries_dedupe ON timetable_entries (branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, room)"
                )

        with pg_conn:
            with pg_conn.cursor() as cur:
                # Verify required tables exist on Postgres
                for t in TABLES:
                    cur.execute(
                        "SELECT to_regclass(%s)",
                        (t,),
                    )
                    exists = cur.fetchone()[0]
                    if not exists:
                        raise RuntimeError(
                            f"Postgres table '{t}' does not exist. Deploy the app first so init_db() creates tables."
                        )

        _print_row_counts_sqlite(sqlite_conn, TABLES)
        _print_row_counts_postgres(pg_conn, TABLES, label="Postgres row counts before migration")

        copied = {}
        with pg_conn:
            for table in TABLES:
                rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
                if not rows:
                    copied[table] = 0
                    continue

                cols = list(rows[0].keys())
                placeholders = ",".join(["%s"] * len(cols))
                col_list = ",".join(cols)

                # Insert preserving IDs. Use ON CONFLICT DO NOTHING for safety.
                # Assumes each table has primary key 'id' and/or unique constraints.
                with pg_conn.cursor() as cur:
                    for r in rows:
                        values = [r[c] for c in cols]
                        # Most tables have id PK; ON CONFLICT (id) is safe.
                        cur.execute(
                            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
                            values,
                        )

                copied[table] = len(rows)

            # Fix sequences for SERIAL columns so future inserts work.
            for table in TABLES:
                _set_sequence(pg_conn, table)

        print("Done. Rows copied (from SQLite -> Neon PostgreSQL):")
        for t in TABLES:
            print(f"- {t}: {copied.get(t, 0)}")

        _print_row_counts_postgres(pg_conn, TABLES, label="Postgres row counts after migration")

        print("\nMigration complete. Verify that the required record counts are present in Postgres.")
        print("If the app is running on Neon, keep DATABASE_URL set to the correct connection string.")
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
