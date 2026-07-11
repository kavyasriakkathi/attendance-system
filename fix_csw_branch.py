"""
Fix CSW branch student data.
Connects to production PostgreSQL via DATABASE_URL environment variable.
Does NOT print or expose the connection string.

Usage:
  python fix_csw_branch.py            # dry-run report only
  python fix_csw_branch.py --apply    # create backup + apply fixes
"""

import os
import sys
import json
import argparse

def get_connection():
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL environment variable is not set.")
        sys.exit(1)

    # Normalise postgres:// -> postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url += sep + "sslmode=require"

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
        sys.exit(1)

    conn = psycopg2.connect(url)
    conn.autocommit = False
    return conn


def is_valid_csw_enrollment(enrollment):
    """Valid CSW roll numbers: 25TQ1A5601 through 25TQ1A5661"""
    if not enrollment:
        return False
    e = str(enrollment).strip().upper()
    if not e.startswith("25TQ1A56"):
        return False
    suffix = e[8:]
    if not suffix.isdigit():
        return False
    num = int(suffix)
    return 1 <= num <= 61


def main():
    parser = argparse.ArgumentParser(description="Fix CSW branch student assignments")
    parser.add_argument("--apply", action="store_true", help="Apply the fixes (default: dry-run report only)")
    args = parser.parse_args()

    conn = get_connection()
    cur = conn.cursor()
    print("Connected to production database successfully.\n")

    # 1. Find CSW branch
    cur.execute("SELECT id, name FROM branches WHERE UPPER(TRIM(name)) = 'CSW'")
    csw_row = cur.fetchone()
    if not csw_row:
        print("ERROR: CSW branch not found in the database.")
        conn.close()
        return
    csw_id, csw_name = csw_row[0], csw_row[1]
    print(f"CSW Branch: id={csw_id}, name='{csw_name}'")

    # 2. Get ALL students currently assigned to CSW
    cur.execute(
        "SELECT s.id, s.name, s.enrollment, s.branch_id, b.name AS branch_name "
        "FROM students s LEFT JOIN branches b ON s.branch_id = b.id "
        "WHERE s.branch_id = %s ORDER BY s.enrollment",
        (csw_id,),
    )
    csw_students = cur.fetchall()
    print(f"Total students currently in CSW branch: {len(csw_students)}\n")

    # 3. Also find valid CSW enrollments NOT in CSW branch
    cur.execute(
        "SELECT s.id, s.name, s.enrollment, s.branch_id, b.name AS branch_name "
        "FROM students s LEFT JOIN branches b ON s.branch_id = b.id "
        "WHERE s.branch_id != %s OR s.branch_id IS NULL "
        "ORDER BY s.enrollment",
        (csw_id,),
    )
    other_students = cur.fetchall()

    # 4. Build action lists
    to_remove = []  # students in CSW that should NOT be
    to_keep = []    # students in CSW that are valid
    to_move = []    # students NOT in CSW that should be

    for row in csw_students:
        sid, sname, enrollment, bid, bname = row
        if is_valid_csw_enrollment(enrollment):
            to_keep.append(row)
        else:
            to_remove.append(row)

    for row in other_students:
        sid, sname, enrollment, bid, bname = row
        if is_valid_csw_enrollment(enrollment):
            to_move.append(row)

    # 5. Print report
    print("=" * 90)
    print(f"{'Enrollment':<15} | {'Name':<35} | {'Current Branch':<15} | Action")
    print("-" * 90)

    for row in to_keep:
        sid, sname, enrollment, bid, bname = row
        print(f"{enrollment:<15} | {(sname or '')[:35]:<35} | {(bname or 'None'):<15} | ✅ Keep CSW")

    for row in to_remove:
        sid, sname, enrollment, bid, bname = row
        print(f"{enrollment:<15} | {(sname or '')[:35]:<35} | {(bname or 'None'):<15} | ❌ Remove CSW")

    for row in to_move:
        sid, sname, enrollment, bid, bname = row
        print(f"{enrollment:<15} | {(sname or '')[:35]:<35} | {(bname or 'None'):<15} | ➡️  Move to CSW")

    print("=" * 90)
    print(f"\nSummary:")
    print(f"  Valid CSW students (keep):        {len(to_keep)}")
    print(f"  Invalid students to remove:       {len(to_remove)}")
    print(f"  Missing CSW students to add:      {len(to_move)}")
    print(f"  Final CSW count after fix:        {len(to_keep) + len(to_move)}")

    if not to_remove and not to_move:
        print("\n✅ No issues found. CSW branch data is clean.")
        conn.close()
        return

    if not args.apply:
        print("\n⚠️  DRY RUN — no changes made.")
        print("Run with --apply to create backup and apply fixes.")
        conn.close()
        return

    # 6. Create backup before modifying
    print("\n[1/3] Creating backup (students_backup_csw.json)...")
    cur.execute("SELECT id, name, enrollment, branch_id, email FROM students ORDER BY id")
    all_students = cur.fetchall()
    backup = []
    for r in all_students:
        backup.append({
            "id": r[0], "name": r[1], "enrollment": r[2],
            "branch_id": r[3], "email": r[4],
        })
    with open("students_backup_csw.json", "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=2, ensure_ascii=False)
    print(f"  Backed up {len(backup)} student records.")

    # 7. Apply fixes
    print("\n[2/3] Applying fixes...")
    removed_count = 0
    moved_count = 0

    for row in to_remove:
        sid = row[0]
        cur.execute("UPDATE students SET branch_id = NULL WHERE id = %s", (sid,))
        removed_count += 1

    for row in to_move:
        sid = row[0]
        cur.execute("UPDATE students SET branch_id = %s WHERE id = %s", (csw_id, sid))
        moved_count += 1

    conn.commit()
    print(f"  Removed CSW assignment from {removed_count} students.")
    print(f"  Moved {moved_count} students into CSW.")

    # 8. Verify
    print("\n[3/3] Verifying...")
    cur.execute("SELECT COUNT(*) FROM students WHERE branch_id = %s", (csw_id,))
    final_count = cur.fetchone()[0]
    print(f"  CSW branch now has {final_count} students.")

    conn.close()
    print("\n✅ Done! To reverse changes, use the backup file: students_backup_csw.json")


if __name__ == "__main__":
    main()
