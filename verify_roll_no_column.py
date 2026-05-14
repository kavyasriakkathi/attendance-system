#!/usr/bin/env python3
"""
Verify that the roll_no column exists in the students table.
This script checks the database schema after the fixes have been applied.
"""
import os
import sys

# Set up the path to import app
sys.path.insert(0, os.path.dirname(__file__))

from app import get_db, init_db, app

def check_students_columns():
    """Check if students table has the required columns."""
    print("Initializing database...")
    init_db()
    print("✓ Database initialized successfully\n")
    
    db = get_db()
    
    # Get the columns in the students table
    print("Checking students table structure...")
    
    if str(app.config.get("DATABASE", "")).startswith("postgres"):
        print("Using PostgreSQL database")
        result = db.execute("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'students' AND table_schema = 'public'
            ORDER BY ordinal_position
        """).fetchall()
        
        columns = {}
        for row in result:
            col_name = row['column_name']
            col_type = row['data_type']
            columns[col_name] = col_type
    else:
        print("Using SQLite database")
        result = db.execute("PRAGMA table_info(students)").fetchall()
        
        columns = {}
        for row in result:
            col_name = row['name']
            col_type = row['type']
            columns[col_name] = col_type
    
    # Print the columns
    print("\nStudents table columns:")
    print("-" * 40)
    for col_name in sorted(columns.keys()):
        col_type = columns[col_name]
        print(f"  ✓ {col_name:20} {col_type}")
    
    print("-" * 40)
    
    # Check for required columns
    required_columns = ['id', 'name', 'enrollment', 'roll_no', 'section', 'branch_id']
    print(f"\nRequired columns check:")
    all_exist = True
    for col in required_columns:
        if col in columns:
            print(f"  ✓ {col}")
        else:
            print(f"  ✗ {col} - MISSING!")
            all_exist = False
    
    if all_exist:
        print("\n✅ All required columns exist! The fix is working.")
    else:
        print("\n❌ Some columns are missing. Manual intervention may be needed.")
    
    # Test a sample query
    print("\nTesting sample query...")
    try:
        students = db.execute(
            "SELECT id, name, enrollment, roll_no, section FROM students LIMIT 1"
        ).fetchall()
        print(f"  ✓ Sample query successful (found {len(students)} records)")
    except Exception as e:
        print(f"  ✗ Sample query failed: {e}")
    
    db.close()

if __name__ == '__main__':
    try:
        check_students_columns()
    except Exception as e:
        print(f"\n❌ Error during verification: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
