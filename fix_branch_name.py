"""
fix_branch_name.py
==================
One-shot migration that renames bad branch names created by the test suite
(COPILOT_BRANCH_*) to their correct names.

Usage
-----
# Fix only the specific branch needed in production:
python fix_branch_name.py --old "COPILOT_BRANCH_d3169ca1" --new "ECE-B"

# Fix all COPILOT_BRANCH_* entries interactively:
python fix_branch_name.py --all

# Dry-run (no writes):
python fix_branch_name.py --old "COPILOT_BRANCH_d3169ca1" --new "ECE-B" --dry-run

The script connects to whichever database the app is configured to use
(DATABASE_URL env var for PostgreSQL, or the local attendance.db SQLite file).
"""

import argparse
import os
import sys

# ── Load environment (.env file support) ──────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not required

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def get_connection():
    """Return (conn, placeholder, db_type) for the configured database."""
    if DATABASE_URL.startswith("postgres"):
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            print("ERROR: psycopg2 is not installed. Run: pip install psycopg2-binary")
            sys.exit(1)
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn, "%s", "postgresql"
    else:
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), "attendance.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn, "?", "sqlite"


def list_copilot_branches(conn, placeholder):
    cur = conn.cursor()
    cur.execute("SELECT id, name, location FROM branches WHERE name LIKE 'COPILOT_BRANCH_%'")
    rows = cur.fetchall()
    return rows


def rename_branch(conn, placeholder, old_name: str, new_name: str, dry_run: bool):
    """
    Rename branch `old_name` -> `new_name`.

    If `new_name` already exists the existing row is kept and the old one is
    deleted (students are re-pointed to the surviving row first).
    """
    cur = conn.cursor()

    # Find the source (old) branch
    cur.execute(
        f"SELECT id FROM branches WHERE name = {placeholder}",
        (old_name,),
    )
    old_row = cur.fetchone()
    if old_row is None:
        print(f"  [SKIP] Branch '{old_name}' not found in branches table.")
        return False

    old_id = old_row[0] if isinstance(old_row, tuple) else old_row["id"]

    # Find whether target name already exists
    cur.execute(
        f"SELECT id FROM branches WHERE name = {placeholder}",
        (new_name,),
    )
    new_row = cur.fetchone()

    if new_row is not None:
        # Merge: re-point students from old_id -> existing new_id, then delete old row
        new_id = new_row[0] if isinstance(new_row, tuple) else new_row["id"]
        print(f"  Branch '{new_name}' already exists (id={new_id}).")
        print(f"  Will re-point all students from id={old_id} -> id={new_id}, then delete id={old_id}.")

        if not dry_run:
            cur.execute(
                f"UPDATE students SET branch_id = {placeholder} WHERE branch_id = {placeholder}",
                (new_id, old_id),
            )
            print(f"    Updated students.branch_id: {cur.rowcount} rows")

            # Also update attendance.branch_id if column exists
            try:
                cur.execute(
                    f"UPDATE attendance SET branch_id = {placeholder} WHERE branch_id = {placeholder}",
                    (new_id, old_id),
                )
                print(f"    Updated attendance.branch_id: {cur.rowcount} rows")
            except Exception:
                pass  # attendance may not have branch_id column

            cur.execute(
                f"DELETE FROM branches WHERE id = {placeholder}",
                (old_id,),
            )
            print(f"    Deleted old branch row id={old_id}")
            conn.commit()
            print(f"  [OK] Merged '{old_name}' into existing '{new_name}'.")
        else:
            print(f"  [DRY-RUN] Would merge '{old_name}' (id={old_id}) into '{new_name}' (id={new_id}).")
    else:
        # Simple rename: just UPDATE the name column
        print(f"  Renaming '{old_name}' -> '{new_name}' (id={old_id}).")
        if not dry_run:
            cur.execute(
                f"UPDATE branches SET name = {placeholder} WHERE id = {placeholder}",
                (new_name, old_id),
            )
            conn.commit()
            print(f"  [OK] Renamed successfully.")
        else:
            print(f"  [DRY-RUN] Would UPDATE branches SET name='{new_name}' WHERE id={old_id}.")

    return True


def main():
    parser = argparse.ArgumentParser(description="Fix bad COPILOT_BRANCH_* branch names.")
    parser.add_argument("--old",     help="Exact old branch name to rename (e.g. COPILOT_BRANCH_d3169ca1)")
    parser.add_argument("--new",     help="New branch name (e.g. ECE-B)")
    parser.add_argument("--all",     action="store_true", help="Interactively rename all COPILOT_BRANCH_* entries")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Print what would happen without writing")
    args = parser.parse_args()

    if not args.old and not args.all:
        parser.print_help()
        sys.exit(1)

    conn, placeholder, db_type = get_connection()
    print(f"Connected to {db_type} database.")
    if args.dry_run:
        print("*** DRY-RUN MODE — no changes will be written ***\n")

    if args.all:
        rows = list_copilot_branches(conn, placeholder)
        if not rows:
            print("No COPILOT_BRANCH_* entries found. Nothing to do.")
            conn.close()
            return

        print(f"Found {len(rows)} COPILOT_BRANCH_* branch(es):\n")
        for row in rows:
            if isinstance(row, tuple):
                bid, bname, bloc = row[0], row[1], row[2]
            else:
                bid, bname, bloc = row["id"], row["name"], row["location"]
            print(f"  id={bid}  name={bname!r}  location={bloc!r}")

        print()
        for row in rows:
            if isinstance(row, tuple):
                bid, bname = row[0], row[1]
            else:
                bid, bname = row["id"], row["name"]
            new_name = input(f"Enter replacement for '{bname}' (or press Enter to skip): ").strip()
            if new_name:
                rename_branch(conn, placeholder, bname, new_name, args.dry_run)
            else:
                print(f"  Skipping '{bname}'.")
    else:
        rename_branch(conn, placeholder, args.old, args.new, args.dry_run)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
