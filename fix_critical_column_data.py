#!/usr/bin/env python3
"""
Database Data Quality Remediation Script
Fixes missing and empty values in critical columns.
"""

import os
import sqlite3
from dotenv import load_dotenv

# Load environment
load_dotenv(override=False)

# Get database path
db_env = os.environ.get("DATABASE_URL")
if db_env and db_env.startswith("postgres"):
    print("[ERROR] PostgreSQL remediation not yet implemented.")
    print("[ERROR] Please manually run the UPDATE queries provided below.")
    import sys
    sys.exit(1)
else:
    app_dir = os.path.dirname(os.path.abspath(__file__))
    database_path = os.path.abspath(os.path.join(app_dir, "attendance.db"))

print(f"Database: {database_path}\n")

# Connect to database
conn = sqlite3.connect(database_path)
conn.row_factory = sqlite3.Row
db = conn.cursor()

print("="*80)
print("DATA QUALITY REMEDIATION")
print("="*80 + "\n")

# ============================================================================
# 1. Fix students.section empty strings
# ============================================================================
print("\n1️⃣  Fixing students.section empty strings...")
print("-" * 80)

try:
    result = db.execute(
        "SELECT id, branch_id FROM students WHERE TRIM(section) = ''"
    ).fetchall()
    
    if result:
        print(f"Found {len(result)} students with empty section:")
        for row in result:
            student_id = row["id"]
            branch_id = row["branch_id"]
            print(f"  - Student ID {student_id} (Branch {branch_id})")
        
        print("\nSetting section = 'A' for all students with empty section...")
        db.execute(
            "UPDATE students SET section = 'A' WHERE TRIM(section) = ''"
        )
        conn.commit()
        print("✅ Successfully updated students.section\n")
    else:
        print("✅ No empty sections found\n")
except Exception as e:
    print(f"❌ Failed to fix students.section: {e}\n")
    conn.rollback()

# ============================================================================
# 2. Fix teachers.subject_id NULL
# ============================================================================
print("\n2️⃣  Fixing teachers.subject_id NULL values...")
print("-" * 80)

try:
    result = db.execute(
        "SELECT id, name, branch_id FROM teachers WHERE subject_id IS NULL"
    ).fetchall()
    
    if result:
        print(f"Found {len(result)} teachers with NULL subject_id:")
        for row in result:
            teacher_id = row["id"]
            teacher_name = row["name"]
            branch_id = row["branch_id"]
            
            print(f"  - Teacher ID {teacher_id} ({teacher_name}, Branch {branch_id})")
            
            # Find the first subject for this branch
            subject_result = db.execute(
                "SELECT id FROM subjects WHERE branch_id = ? ORDER BY id LIMIT 1",
                (branch_id,)
            ).fetchone()
            
            if subject_result:
                subject_id = subject_result["id"]
                db.execute(
                    "UPDATE teachers SET subject_id = ? WHERE id = ?",
                    (subject_id, teacher_id)
                )
                print(f"    → Assigned Subject ID {subject_id}")
            else:
                print(f"    → ⚠️  No subjects found for Branch {branch_id}")
        
        conn.commit()
        print("\n✅ Successfully updated teachers.subject_id\n")
    else:
        print("✅ No NULL subject_ids found\n")
except Exception as e:
    print(f"❌ Failed to fix teachers.subject_id: {e}\n")
    conn.rollback()

# ============================================================================
# 3. Verify fixes
# ============================================================================
print("\n3️⃣  Verifying fixes...")
print("-" * 80)

try:
    empty_sections = db.execute(
        "SELECT COUNT(*) as cnt FROM students WHERE TRIM(section) = ''"
    ).fetchone()["cnt"]
    
    null_subject_ids = db.execute(
        "SELECT COUNT(*) as cnt FROM teachers WHERE subject_id IS NULL"
    ).fetchone()["cnt"]
    
    print(f"Empty sections remaining: {empty_sections}")
    print(f"NULL subject_ids remaining: {null_subject_ids}")
    
    if empty_sections == 0 and null_subject_ids == 0:
        print("\n✅ ALL DATA QUALITY ISSUES RESOLVED!\n")
    else:
        print("\n⚠️  Some issues remain - check above for details\n")
except Exception as e:
    print(f"❌ Verification failed: {e}\n")

conn.close()

print("="*80)
print("Remediation complete!")
print("="*80 + "\n")
