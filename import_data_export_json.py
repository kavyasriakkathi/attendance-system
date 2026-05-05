import json
import os
from pathlib import Path
from urllib.parse import urlparse


def _generate_password_hash(password: str) -> str:
    # Import on demand so the script doesn't fail at import-time.
    from werkzeug.security import generate_password_hash

    return generate_password_hash(password)


TABLES = [
    "branches",
    "subjects",
    "students",
    "attendance",
]


def _ensure_sslmode(url: str) -> str:
    if "sslmode=" in url:
        return url
    is_render = bool(os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME"))
    if not is_render:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}sslmode=require"


def _set_sequence(conn, table: str, id_col: str = "id") -> None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COALESCE(MAX({id_col}), 0) FROM {table}")
        max_id = int(cur.fetchone()[0])
        cur.execute(
            "SELECT setval(pg_get_serial_sequence(%s, %s), %s, %s)",
            (table, id_col, max_id, bool(max_id)),
        )


def main() -> int:
    # Import on demand so the script can be imported without Postgres libs installed.
    import psycopg2

    export_path = Path(os.environ.get("EXPORT_JSON", "scratch/data_export.json"))
    pg_url = os.environ.get("DATABASE_URL")

    if not pg_url:
        print("ERROR: DATABASE_URL is not set. Put your Render Postgres External Database URL in DATABASE_URL.")
        return 2

    if pg_url.startswith("postgres://"):
        pg_url = pg_url.replace("postgres://", "postgresql://", 1)

    pg_url = _ensure_sslmode(pg_url)

    if not export_path.exists():
        print(f"ERROR: Export JSON not found at {export_path}")
        return 2

    data = json.loads(export_path.read_text(encoding="utf-8"))

    parsed = urlparse(pg_url)
    print(f"Importing {export_path} -> Postgres host={parsed.hostname} port={parsed.port} db={parsed.path.lstrip('/')}")

    conn = psycopg2.connect(pg_url, connect_timeout=10)
    try:
        with conn:
            with conn.cursor() as cur:
                # Ensure tables exist
                required = TABLES + ["users", "settings"]
                for t in required:
                    cur.execute("SELECT to_regclass(%s)", (t,))
                    exists = cur.fetchone()[0]
                    if not exists:
                        raise RuntimeError(
                            f"Postgres table '{t}' does not exist. Deploy the app first so init_db() creates tables."
                        )

        inserted = {t: 0 for t in TABLES}

        with conn:
            # branches
            for row in data.get("branches", []):
                conn.cursor().execute(
                    "INSERT INTO branches (id, name, location) VALUES (%s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (row.get("id"), row.get("name"), row.get("location")),
                )
                inserted["branches"] += 1

            # subjects
            for row in data.get("subjects", []):
                conn.cursor().execute(
                    "INSERT INTO subjects (id, name, branch_id) VALUES (%s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (row.get("id"), row.get("name"), row.get("branch_id")),
                )
                inserted["subjects"] += 1

            # students
            for row in data.get("students", []):
                conn.cursor().execute(
                    "INSERT INTO students (id, name, enrollment, branch_id, email) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        row.get("id"),
                        row.get("name"),
                        row.get("enrollment"),
                        row.get("branch_id"),
                        row.get("email"),
                    ),
                )
                inserted["students"] += 1

            # attendance
            for row in data.get("attendance", []):
                conn.cursor().execute(
                    "INSERT INTO attendance (id, student_id, branch_id, subject_id, date, status, note) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        row.get("id"),
                        row.get("student_id"),
                        row.get("branch_id"),
                        row.get("subject_id"),
                        row.get("date"),
                        row.get("status"),
                        row.get("note"),
                    ),
                )
                inserted["attendance"] += 1

            # Create student user accounts if missing.
            # username = enrollment, password = last 4 chars of enrollment (same as your app behavior).
            for row in data.get("students", []):
                enrollment = (row.get("enrollment") or "").strip()
                student_id = row.get("id")
                if not enrollment or not student_id:
                    continue
                default_password = enrollment[-4:] if len(enrollment) >= 4 else enrollment
                conn.cursor().execute(
                    "INSERT INTO users (username, password, role, student_id) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (username) DO NOTHING",
                    (enrollment, _generate_password_hash(default_password), "student", student_id),
                )

            # Fix sequences
            for t in TABLES + ["users", "settings"]:
                _set_sequence(conn, t)

        print("Import finished.")
        print("Attempted inserts from JSON:")
        for t in TABLES:
            print(f"- {t}: {inserted[t]}")
        print("\nNext: open your Render app and visit /admin/check-db to confirm counts.")
        return 0

    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
