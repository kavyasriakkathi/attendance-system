import sqlite3
import shutil
from pathlib import Path
import argparse

def get_csw_branch_id(conn):
    row = conn.execute("SELECT id, name FROM branches WHERE UPPER(name) = 'CSW'").fetchone()
    if row:
        return row['id']
    return None

def is_valid_csw_roll(enrollment):
    if not enrollment:
        return False
    enrollment = enrollment.strip().upper()
    if enrollment.startswith("25TQ1A56"):
        # Check if the last two digits are between 01 and 61
        suffix = enrollment[8:]
        if suffix.isdigit():
            num = int(suffix)
            if 1 <= num <= 61:
                return True
    return False

def check_csw_students():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply the changes to the database")
    args = parser.parse_args()

    db_path = Path("attendance.db")
    if not db_path.exists():
        print("Error: attendance.db not found.")
        return

    if args.apply:
        backup_path = Path("attendance_backup_csw.db")
        shutil.copy2(db_path, backup_path)
        print(f"Backup created at: {backup_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    csw_id = get_csw_branch_id(conn)
    if not csw_id:
        print("Error: CSW branch not found in branches table.")
        return

    print(f"CSW Branch ID: {csw_id}")

    # Find all students currently in CSW branch
    rows = conn.execute("SELECT id, name, enrollment, branch_id FROM students WHERE branch_id = ?", (csw_id,)).fetchall()
    
    # We should also check if any valid CSW students are NOT in CSW branch
    # (The requirement doesn't explicitly state to move them to CSW, but we'll print them just in case)
    other_rows = conn.execute("SELECT id, name, enrollment, branch_id FROM students WHERE branch_id != ? OR branch_id IS NULL", (csw_id,)).fetchall()
    
    actions = []
    
    for row in rows:
        enrollment = row['enrollment']
        if is_valid_csw_roll(enrollment):
            actions.append((row, "CSW", "Keep CSW"))
        else:
            actions.append((row, "CSW", "Remove CSW"))
            
    for row in other_rows:
        if is_valid_csw_roll(row['enrollment']):
            # Should they be added to CSW?
            actions.append((row, "Other/None", "Move to CSW (Optional)"))

    print("\n--- REPORT ---")
    print(f"{'Enrollment':<15} | {'Name':<30} | {'Current Branch':<15} | Action")
    print("-" * 85)
    
    for row, current_branch, action in actions:
        if action != "Keep CSW": # highlight changes
            print(f"{row['enrollment']:<15} | {row['name'][:30]:<30} | {current_branch:<15} | {action}")

    to_remove = [r[0] for r in actions if r[2] == "Remove CSW"]
    
    print("\nSummary:")
    print(f"Total students in CSW branch: {len(rows)}")
    print(f"Valid CSW students: {sum(1 for a in actions if a[2] == 'Keep CSW')}")
    print(f"Students to remove from CSW: {len(to_remove)}")
    
    if args.apply:
        print("\nApplying updates...")
        for row in to_remove:
            conn.execute("UPDATE students SET branch_id = NULL WHERE id = ?", (row['id'],))
        conn.commit()
        print("Update complete.")
    else:
        print("\nRun with --apply to execute the updates.")

    conn.close()

if __name__ == '__main__':
    check_csw_students()
