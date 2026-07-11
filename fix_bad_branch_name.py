#!/usr/bin/env python
"""
Migration script to fix bad branch name COPILOT_BRANCH_d3169ca1 → ECE-B
"""
import os
import sys

# Add the project directory to sys.path
sys.path.insert(0, os.path.dirname(__file__))

from app import app, get_db, get_placeholder, row_get

def migrate_branch_name():
    """Fix COPILOT_BRANCH_d3169ca1 → ECE-B"""
    
    db = get_db()
    placeholder = get_placeholder()
    
    try:
        # Find the bad branch
        bad_branch_row = db.execute(
            f"SELECT id, name FROM branches WHERE name = {placeholder}",
            ("COPILOT_BRANCH_d3169ca1",)
        ).fetchone()
        
        if not bad_branch_row:
            print("✓ No bad branch name found. Database is clean.")
            return
        
        bad_branch_id = row_get(bad_branch_row, "id")
        bad_branch_name = row_get(bad_branch_row, "name")
        print(f"Found bad branch: id={bad_branch_id}, name={bad_branch_name}")
        
        # Check if ECE-B already exists
        ece_b_branch = db.execute(
            f"SELECT id, name FROM branches WHERE UPPER(TRIM(name)) = {placeholder}",
            ("ECE-B",)
        ).fetchone()
        
        if ece_b_branch:
            ece_b_id = row_get(ece_b_branch, "id")
            print(f"ECE-B branch already exists with id={ece_b_id}. Merging data...")
            
            # Merge all foreign key references from bad branch to ECE-B
            tables_to_update = [
                ("students", "branch_id"),
                ("subjects", "branch_id"),
                ("timetable_entries", "branch_id"),
                ("attendance", "branch_id"),
                ("teacher_branches", "branch_id"),
                ("teachers", "branch_id"),
            ]
            
            for table, col in tables_to_update:
                try:
                    db.execute(
                        f"UPDATE {table} SET {col} = {placeholder} WHERE {col} = {placeholder}",
                        (ece_b_id, bad_branch_id)
                    )
                    affected = db.execute(
                        f"SELECT COUNT(*) as cnt FROM {table} WHERE {col} = {placeholder}",
                        (ece_b_id,)
                    ).fetchone()
                    count = row_get(affected, "cnt") or 0
                    print(f"  ✓ Updated {table}: {count} rows now pointing to ECE-B")
                except Exception as e:
                    print(f"  ⚠ Table {table} update failed (may not exist): {repr(e)}")
            
            # Delete the bad branch record
            try:
                db.execute(
                    f"DELETE FROM branches WHERE id = {placeholder}",
                    (bad_branch_id,)
                )
                print(f"  ✓ Deleted branch record id={bad_branch_id}")
            except Exception as e:
                print(f"  ✗ Failed to delete branch: {repr(e)}")
        else:
            # Simply rename the branch
            print("ECE-B does not exist. Renaming branch...")
            db.execute(
                f"UPDATE branches SET name = {placeholder} WHERE id = {placeholder}",
                ("ECE-B", bad_branch_id)
            )
            print(f"  ✓ Renamed branch to ECE-B")
        
        db.commit()
        
        # Verify the fix
        students_count = db.execute(
            f"SELECT COUNT(*) as cnt FROM students WHERE branch_id = (SELECT id FROM branches WHERE name = {placeholder})",
            ("ECE-B",)
        ).fetchone()
        count = row_get(students_count, "cnt") or 0
        print(f"\n✓ Migration complete! ECE-B now has {count} students")
        
    except Exception as e:
        print(f"✗ Migration failed: {repr(e)}")
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
    print("=" * 60)
    print("Fixing bad branch name: COPILOT_BRANCH_d3169ca1 → ECE-B")
    print("=" * 60)
    migrate_branch_name()
    print("=" * 60)
