#!/usr/bin/env python
"""
Migration script to fix students incorrectly assigned to CSW → move to CIVIL
Targets: 25TQ1A030x enrollment pattern
"""
import os
import sys

# Add the project directory to sys.path
sys.path.insert(0, os.path.dirname(__file__))

from app import app, get_db, get_placeholder, row_get

def fix_civil_branch():
    """Move incorrectly assigned CSW students to CIVIL branch"""
    
    db = get_db()
    placeholder = get_placeholder()
    
    try:
        print("=" * 60)
        print("FIXING: Move CSW students to CIVIL branch")
        print("=" * 60)
        
        # Find CIVIL branch
        civil_branch = db.execute(
            f"SELECT id, name FROM branches WHERE UPPER(TRIM(name)) = {placeholder}",
            ("CIVIL",)
        ).fetchone()
        
        if not civil_branch:
            print("✗ CIVIL branch not found in database!")
            return
        
        civil_id = row_get(civil_branch, "id")
        print(f"✓ Found CIVIL branch: id={civil_id}")
        
        # Find CSW branch
        csw_branch = db.execute(
            f"SELECT id, name FROM branches WHERE UPPER(TRIM(name)) = {placeholder}",
            ("CSW",)
        ).fetchone()
        
        if not csw_branch:
            print("✗ CSW branch not found in database!")
            return
        
        csw_id = row_get(csw_branch, "id")
        print(f"✓ Found CSW branch: id={csw_id}")
        
        # Find students with 25TQ1A030x pattern in CSW
        students_to_move = db.execute(
            f"""SELECT id, enrollment, name FROM students 
               WHERE branch_id = {placeholder} 
               AND enrollment LIKE {placeholder}
               ORDER BY enrollment""",
            (csw_id, "25TQ1A030%")
        ).fetchall()
        
        if not students_to_move:
            print(f"✓ No students found with 25TQ1A030x pattern in CSW branch")
            return
        
        print(f"\n✓ Found {len(students_to_move)} students to move:")
        for student in students_to_move:
            sid = row_get(student, "id")
            enrollment = row_get(student, "enrollment")
            name = row_get(student, "name")
            print(f"    - {enrollment} | {name}")
        
        # Move them to CIVIL
        print(f"\nMoving {len(students_to_move)} students to CIVIL...")
        db.execute(
            f"""UPDATE students SET branch_id = {placeholder}
               WHERE branch_id = {placeholder} 
               AND enrollment LIKE {placeholder}""",
            (civil_id, csw_id, "25TQ1A030%")
        )
        
        db.commit()
        
        # Verify the move
        verify = db.execute(
            f"""SELECT COUNT(*) as cnt FROM students 
               WHERE branch_id = {placeholder}
               AND enrollment LIKE {placeholder}""",
            (civil_id, "25TQ1A030%")
        ).fetchone()
        
        count = row_get(verify, "cnt") or 0
        print(f"\n✓ Migration complete! {count} students now in CIVIL branch")
        
        # Show updated list
        print("\n" + "=" * 60)
        print("UPDATED STUDENTS:")
        print("=" * 60)
        updated_students = db.execute(
            f"""SELECT s.id, s.enrollment, s.name, b.name as branch_name 
               FROM students s 
               LEFT JOIN branches b ON s.branch_id = b.id 
               WHERE s.enrollment LIKE {placeholder}
               ORDER BY s.enrollment""",
            ("25TQ1A030%",)
        ).fetchall()
        
        for student in updated_students:
            eid = row_get(student, "id")
            enrollment = row_get(student, "enrollment")
            name = row_get(student, "name")
            branch = row_get(student, "branch_name")
            print(f"  {enrollment} | {name:30s} | {branch}")
        
        print("=" * 60)
        
    except Exception as e:
        print(f"✗ Migration failed: {repr(e)}")
        import traceback
        traceback.print_exc()
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass

if __name__ == "__main__":
    fix_civil_branch()
