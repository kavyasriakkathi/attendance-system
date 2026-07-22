#!/usr/bin/env python
"""
Check students currently in CSW branch
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app import app, get_db, get_placeholder, row_get

def check_csw_students():
    """Check all students in CSW branch"""
    
    db = get_db()
    placeholder = get_placeholder()
    
    try:
        print("=" * 60)
        print("STUDENTS CURRENTLY IN CSW BRANCH:")
        print("=" * 60)
        
        csw_branch = db.execute(
            f"SELECT id, name FROM branches WHERE UPPER(TRIM(name)) = {placeholder}",
            ("CSW",)
        ).fetchone()
        
        if not csw_branch:
            print("CSW branch not found!")
            return
        
        csw_id = row_get(csw_branch, "id")
        print(f"CSW Branch ID: {csw_id}\n")
        
        # Get all students in CSW
        students = db.execute(
            f"""SELECT s.id, s.enrollment, s.name 
               FROM students s 
               WHERE s.branch_id = {placeholder}
               ORDER BY s.enrollment""",
            (csw_id,)
        ).fetchall()
        
        if not students:
            print("No students in CSW branch")
        else:
            print(f"Total: {len(students)} students\n")
            for student in students:
                sid = row_get(student, "id")
                enrollment = row_get(student, "enrollment")
                name = row_get(student, "name")
                print(f"  {enrollment} | {name}")
        
        print("\n" + "=" * 60)
        print("ALL BRANCHES AND THEIR STUDENTS:")
        print("=" * 60)
        
        branches = db.execute(
            "SELECT id, name FROM branches ORDER BY id"
        ).fetchall()
        
        for branch in branches:
            bid = row_get(branch, "id")
            bname = row_get(branch, "name")
            count_result = db.execute(
                f"SELECT COUNT(*) as cnt FROM students WHERE branch_id = {placeholder}",
                (bid,)
            ).fetchone()
            count = row_get(count_result, "cnt") or 0
            if count > 0:
                print(f"\n{bname} (ID={bid}): {count} students")
                students_in_branch = db.execute(
                    f"SELECT enrollment, name FROM students WHERE branch_id = {placeholder} ORDER BY enrollment LIMIT 10",
                    (bid,)
                ).fetchall()
                for st in students_in_branch:
                    enroll = row_get(st, "enrollment")
                    name = row_get(st, "name")
                    print(f"    - {enroll} | {name}")
        
        print("\n" + "=" * 60)
        
    except Exception as e:
        print(f"Error: {repr(e)}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            db.close()
        except Exception:
            pass

if __name__ == "__main__":
    check_csw_students()
