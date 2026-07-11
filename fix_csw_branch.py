import os
import sys
import json
import argparse
from pathlib import Path

# Import app modules
from app import app, get_db

def is_valid_csw_roll(enrollment):
    if not enrollment:
        return False
    enrollment = str(enrollment).strip().upper()
    if enrollment.startswith("25TQ1A56"):
        suffix = enrollment[8:]
        if suffix.isdigit():
            num = int(suffix)
            if 1 <= num <= 61:
                return True
    return False

def backup_students(db, is_postgres):
    print("Creating backup of students data to students_backup.json...")
    rows = db.execute("SELECT id, name, enrollment, branch_id, email FROM students").fetchall()
    backup_data = []
    for r in rows:
        backup_data.append({
            "id": r["id"] if not isinstance(r, tuple) else r[0],
            "name": r["name"] if not isinstance(r, tuple) else r[1],
            "enrollment": r["enrollment"] if not isinstance(r, tuple) else r[2],
            "branch_id": r["branch_id"] if not isinstance(r, tuple) else r[3],
            "email": r["email"] if not isinstance(r, tuple) else r[4]
        })
    with open("students_backup.json", "w") as f:
        json.dump(backup_data, f, indent=2)
    print(f"Backup created with {len(backup_data)} students.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply the fixes to the database")
    args = parser.parse_args()

    with app.app_context():
        db = get_db()
        db_url = os.environ.get("DATABASE_URL") or app.config.get('DATABASE', '')
        is_postgres = str(db_url).startswith("postgres")
        
        csw_row = db.execute("SELECT id, name FROM branches WHERE UPPER(name) = 'CSW'").fetchone()
        if not csw_row:
            print("Error: CSW branch not found in the database.")
            return
            
        csw_id = csw_row["id"] if not isinstance(csw_row, tuple) else csw_row[0]
        
        # Get all students
        rows = db.execute("SELECT s.id, s.name, s.enrollment, s.branch_id, b.name as branch_name FROM students s LEFT JOIN branches b ON s.branch_id = b.id").fetchall()
        
        actions = []
        csw_current_count = 0
        
        for r in rows:
            sid = r["id"] if not isinstance(r, tuple) else r[0]
            name = r["name"] if not isinstance(r, tuple) else r[1]
            enr = r["enrollment"] if not isinstance(r, tuple) else r[2]
            bid = r["branch_id"] if not isinstance(r, tuple) else r[3]
            bname = r["branch_name"] if not isinstance(r, tuple) else r[4]
            
            is_csw_branch = (bid == csw_id)
            if is_csw_branch:
                csw_current_count += 1
                
            valid_csw_roll = is_valid_csw_roll(enr)
            
            if is_csw_branch and not valid_csw_roll:
                actions.append((sid, enr, name, bname, "Remove CSW"))
            elif not is_csw_branch and valid_csw_roll:
                actions.append((sid, enr, name, bname or "None", "Move to CSW"))
                
        print(f"\nCurrent CSW branch count: {csw_current_count} students\n")
        
        if not actions:
            print("No branch assignment issues found for CSW.")
            return
            
        print("--- REPORT ---")
        print(f"{'Enrollment':<15} | {'Name':<30} | {'Current Branch':<15} | Action")
        print("-" * 85)
        
        for sid, enr, name, bname, action in actions:
            print(f"{enr:<15} | {name[:30]:<30} | {bname:<15} | {action}")
            
        print("\nSummary:")
        removes = [a for a in actions if a[4] == "Remove CSW"]
        adds = [a for a in actions if a[4] == "Move to CSW"]
        print(f"Students to remove from CSW: {len(removes)}")
        print(f"Students to add to CSW: {len(adds)}")
        
        if args.apply:
            # 1. Create a backup
            print("\n[1/2] Creating backup before modifying data...")
            backup_students(db, is_postgres)
            
            # 2. Update database
            print("\n[2/2] Updating database...")
            placeholder = "%s" if is_postgres else "?"
            for sid, enr, name, bname, action in actions:
                if action == "Remove CSW":
                    # Remove from CSW (set branch_id to NULL so they don't incorrectly appear in CSW)
                    db.execute(f"UPDATE students SET branch_id = NULL WHERE id = {placeholder}", (sid,))
                elif action == "Move to CSW":
                    db.execute(f"UPDATE students SET branch_id = {placeholder} WHERE id = {placeholder}", (csw_id, sid))
            
            db.commit()
            print("\nDone! Database updated successfully.")
            print("To reverse these changes, you can use the data in students_backup.json")
        else:
            print("\nRun with --apply to execute the updates.")

if __name__ == '__main__':
    main()
