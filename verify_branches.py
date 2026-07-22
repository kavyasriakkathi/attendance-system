#!/usr/bin/env python
"""
Verify current branch state and student assignments
"""
import os
import sys

# Add the project directory to sys.path
sys.path.insert(0, os.path.dirname(__file__))

from app import app, get_db, get_placeholder, row_get

def verify_branches():
    """Verify branches and student assignments"""
    
    db = get_db()
    placeholder = get_placeholder()
    
    try:
        print("=" * 60)
        print("BRANCHES IN DATABASE:")
        print("=" * 60)
        
        branches = db.execute("SELECT id, name FROM branches ORDER BY id").fetchall()
        if not branches:
            print("No branches found!")
        else:
            for branch in branches:
                branch_id = row_get(branch, "id")
                branch_name = row_get(branch, "name")
                
                # Count students in each branch
                students_count = db.execute(
                    f"SELECT COUNT(*) as cnt FROM students WHERE branch_id = {placeholder}",
                    (branch_id,)
                ).fetchone()
                count = row_get(students_count, "cnt") or 0
                print(f"  ID={branch_id:3d} | Name={branch_name:30s} | Students={count}")
        
        print("\n" + "=" * 60)
        print("SAMPLE STUDENTS (first 5):")
        print("=" * 60)
        
        students = db.execute(
            """SELECT s.id, s.enrollment, s.name, b.name as branch_name 
               FROM students s 
               LEFT JOIN branches b ON s.branch_id = b.id 
               ORDER BY s.id LIMIT 5"""
        ).fetchall()
        
        if not students:
            print("No students found!")
        else:
            for student in students:
                eid = row_get(student, "id")
                enroll = row_get(student, "enrollment")
                name = row_get(student, "name")
                branch = row_get(student, "branch_name")
                print(f"  ID={eid} | Enrollment={enroll} | Name={name:20s} | Branch={branch}")
        
        print("\n" + "=" * 60)
        print("VERIFICATION COMPLETE")
        print("=" * 60)
        
    except Exception as e:
        print(f"✗ Verification failed: {repr(e)}")
    finally:
        try:
            db.close()
        except Exception:
            pass

if __name__ == "__main__":
    verify_branches()
