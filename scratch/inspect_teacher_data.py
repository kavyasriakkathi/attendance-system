import os
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

import app as app_module
from app import app, get_db, row_get

def inspect():
    with app.app_context():
        db = get_db()
        print("=== DATABASE PROVIDER INFORMATION ===")
        db_config = str(app.config.get("DATABASE", ""))
        print(f"DATABASE CONFIG: {db_config[:60]}...")
        
        tables = ["users", "teachers", "teacher_subject_assignments", "teacher_subjects", "teacher_branches"]
        
        print("\n=== 1. ROW COUNTS ===")
        for table in tables:
            try:
                res = db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                cnt = row_get(res, "count")
                print(f"Table '{table}': {cnt} rows")
            except Exception as e:
                print(f"Table '{table}': Error querying count -> {repr(e)}")

        print("\n=== 2 & 3. ALL TEACHER RECORDS FROM EACH TABLE ===")
        
        print("\n--- A. users table (role = 'teacher' or all users if few) ---")
        try:
            users = db.execute("SELECT id, username, role FROM users WHERE role = 'teacher'").fetchall()
            print(f"Total users with role='teacher': {len(users)}")
            for u in users:
                print(dict(u) if hasattr(u, "keys") else u)
        except Exception as e:
            print("Error reading users:", repr(e))

        print("\n--- B. teachers table ---")
        try:
            teachers = db.execute("SELECT * FROM teachers").fetchall()
            print(f"Total teachers table records: {len(teachers)}")
            for t in teachers:
                print(dict(t) if hasattr(t, "keys") else t)
        except Exception as e:
            print("Error reading teachers:", repr(e))

        print("\n--- C. teacher_subject_assignments table ---")
        try:
            tsa = db.execute("SELECT * FROM teacher_subject_assignments").fetchall()
            print(f"Total teacher_subject_assignments records: {len(tsa)}")
            for r in tsa:
                print(dict(r) if hasattr(r, "keys") else r)
        except Exception as e:
            print("Error reading teacher_subject_assignments:", repr(e))

        print("\n--- D. teacher_subjects table ---")
        try:
            ts = db.execute("SELECT * FROM teacher_subjects").fetchall()
            print(f"Total teacher_subjects records: {len(ts)}")
            for r in ts:
                print(dict(r) if hasattr(r, "keys") else r)
        except Exception as e:
            print("Error reading teacher_subjects:", repr(e))

        print("\n--- E. teacher_branches table ---")
        try:
            tb = db.execute("SELECT * FROM teacher_branches").fetchall()
            print(f"Total teacher_branches records: {len(tb)}")
            for r in tb:
                print(dict(r) if hasattr(r, "keys") else r)
        except Exception as e:
            print("Error reading teacher_branches:", repr(e))

        print("\n=== 4. ADMIN -> TEACHERS PAGE QUERY INSPECTION ===")
        # Check how Admin -> Teachers page queries teachers
        print("Auditing app.py logic for admin teachers route...")

if __name__ == "__main__":
    inspect()
