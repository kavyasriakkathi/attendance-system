#!/usr/bin/env python3
"""
Database Critical Column Verification Script
Checks for all critical columns that must exist in the database for proper app functionality.
Provides a detailed report of missing, existing, and data integrity issues.
"""

import os
import sys
import sqlite3
from typing import Dict, List, Tuple, Set
from dotenv import load_dotenv

# Load environment
load_dotenv(override=False)

# Get database path
db_env = os.environ.get("DATABASE_URL")
if db_env and db_env.startswith("postgres"):
    if db_env.startswith("postgres://"):
        db_env = db_env.replace("postgres://", "postgresql://", 1)
    print("[ERROR] PostgreSQL verification not yet implemented in this script.")
    print("[ERROR] Please verify manually using: psql <db_url> -c '\\d+ students' etc.")
    sys.exit(1)
else:
    app_dir = os.path.dirname(os.path.abspath(__file__))
    database_path = os.path.abspath(os.path.join(app_dir, "attendance.db"))

print(f"Database: {database_path}")
if not os.path.exists(database_path):
    print("[ERROR] Database file not found!")
    sys.exit(1)

# Connect to database
conn = sqlite3.connect(database_path)
conn.row_factory = sqlite3.Row
db = conn.cursor()

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_table_columns(table_name: str) -> Set[str]:
    """Get all columns in a table."""
    try:
        result = db.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row["name"] for row in result}
    except Exception as e:
        print(f"[ERROR] Failed to get columns for table {table_name}: {e}")
        return set()

def table_exists(table_name: str) -> bool:
    """Check if a table exists."""
    result = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    ).fetchone()
    return result is not None

def get_row_count(table_name: str) -> int:
    """Get row count in a table."""
    try:
        result = db.execute(f"SELECT COUNT(*) as cnt FROM {table_name}").fetchone()
        return result["cnt"] if result else 0
    except Exception as e:
        print(f"[ERROR] Failed to count rows in {table_name}: {e}")
        return -1

def check_column_nullable(table_name: str, column_name: str) -> bool:
    """Check if a column is nullable."""
    try:
        result = db.execute(f"PRAGMA table_info({table_name})").fetchall()
        for row in result:
            if row["name"] == column_name:
                return row["notnull"] == 0  # 0 = nullable, 1 = not null
        return None
    except Exception as e:
        print(f"[ERROR] Failed to check nullability of {table_name}.{column_name}: {e}")
        return None

def get_null_count(table_name: str, column_name: str) -> int:
    """Get count of NULL values in a column."""
    try:
        result = db.execute(
            f"SELECT COUNT(*) as cnt FROM {table_name} WHERE {column_name} IS NULL"
        ).fetchone()
        return result["cnt"] if result else 0
    except Exception as e:
        return -1

def get_empty_string_count(table_name: str, column_name: str) -> int:
    """Get count of empty string values in a column."""
    try:
        result = db.execute(
            f"SELECT COUNT(*) as cnt FROM {table_name} WHERE TRIM({column_name}) = ''"
        ).fetchone()
        return result["cnt"] if result else 0
    except Exception as e:
        return -1

# ============================================================================
# CRITICAL COLUMNS DEFINITION
# ============================================================================

CRITICAL_COLUMNS = {
    "students": [
        ("id", "INTEGER", "Primary key", True),
        ("name", "TEXT", "Student name", True),
        ("enrollment", "TEXT", "Enrollment number (unique)", True),
        ("roll_no", "TEXT", "Roll number", False),
        ("branch_id", "INTEGER", "Branch ID (FK)", True),
        ("section", "TEXT", "Section", False),
        ("email", "TEXT", "Student email", False),
        ("parent_email", "TEXT", "Parent email", False),
        ("current_year", "INTEGER", "Current academic year", False),
        ("current_semester", "INTEGER", "Current semester", False),
        ("import_order", "INTEGER", "Import order for data consistency", False),
    ],
    "attendance": [
        ("id", "INTEGER", "Primary key", True),
        ("student_id", "INTEGER", "Student ID (FK)", True),
        ("branch_id", "INTEGER", "Branch ID (FK)", True),
        ("branch_section", "TEXT", "Branch section name", False),
        ("section", "TEXT", "Section", False),
        ("subject_id", "INTEGER", "Subject ID (FK)", False),  # CRITICAL - new
        ("teacher_id", "INTEGER", "Teacher ID (FK)", False),
        ("subject_name", "TEXT", "Subject name (legacy)", False),
        ("date", "TEXT", "Attendance date", True),
        ("status", "TEXT", "Attendance status", True),
        ("note", "TEXT", "Notes", False),
        ("period", "INTEGER", "Period number", False),  # CRITICAL - new
    ],
    "branches": [
        ("id", "INTEGER", "Primary key", True),
        ("name", "TEXT", "Branch name", True),
        ("location", "TEXT", "Branch location", False),
    ],
    "subjects": [
        ("id", "INTEGER", "Primary key", True),
        ("name", "TEXT", "Subject name", True),
        ("branch_id", "INTEGER", "Branch ID (FK)", True),
    ],
    "teachers": [
        ("id", "INTEGER", "Primary key", True),
        ("name", "TEXT", "Teacher name", True),
        ("username", "TEXT", "Username (unique)", True),
        ("password", "TEXT", "Password hash", True),
        ("subject_id", "INTEGER", "Subject ID (FK)", False),
        ("branch_id", "INTEGER", "Default branch ID (FK)", True),
        ("subject_name", "TEXT", "Subject name (legacy)", False),
    ],
    "teacher_branches": [
        ("id", "INTEGER", "Primary key", True),
        ("teacher_id", "INTEGER", "Teacher ID (FK)", True),
        ("branch_id", "INTEGER", "Branch ID (FK)", True),
    ],
}

# ============================================================================
# VERIFICATION REPORT
# ============================================================================

print("\n" + "="*80)
print("DATABASE CRITICAL COLUMN VERIFICATION REPORT")
print("="*80 + "\n")

total_issues = 0
missing_tables = []
missing_columns = []
nullable_issues = []
data_quality_issues = []

for table_name, columns in CRITICAL_COLUMNS.items():
    print(f"\n📊 Table: {table_name}")
    print("-" * 80)
    
    if not table_exists(table_name):
        print(f"  ❌ TABLE MISSING - This table must be created!")
        missing_tables.append(table_name)
        total_issues += 1
        continue
    
    row_count = get_row_count(table_name)
    print(f"  Rows: {row_count}")
    
    existing_columns = get_table_columns(table_name)
    
    for col_name, col_type, description, is_required in columns:
        if col_name not in existing_columns:
            status = "❌ MISSING"
            missing_columns.append((table_name, col_name, col_type))
            total_issues += 1
        else:
            # Check if required column is nullable
            is_nullable = check_column_nullable(table_name, col_name)
            if is_required and is_nullable:
                status = "⚠️  EXISTS (but NULLABLE - should be NOT NULL)"
                nullable_issues.append((table_name, col_name))
                total_issues += 1
            else:
                status = "✓  EXISTS"
            
            # Check data quality for critical columns
            if row_count > 0 and col_name in ["roll_no", "section", "import_order", "subject_id", "period"]:
                null_cnt = get_null_count(table_name, col_name)
                if col_name in ["section", "branch_section"]:
                    empty_cnt = get_empty_string_count(table_name, col_name)
                else:
                    empty_cnt = 0
                
                if null_cnt > 0 or empty_cnt > 0:
                    data_quality_issues.append((table_name, col_name, null_cnt, empty_cnt))
                    status += f"\n    ⚠️  DATA QUALITY: {null_cnt} NULLs, {empty_cnt} empty strings"
                    total_issues += 1
        
        print(f"  {status}")
        print(f"      {col_name}: {col_type} - {description}")

print("\n" + "="*80)
print("SUMMARY")
print("="*80)

print(f"\nTotal Issues Found: {total_issues}")

if missing_tables:
    print(f"\n❌ Missing Tables ({len(missing_tables)}):")
    for table in missing_tables:
        print(f"   - {table}")

if missing_columns:
    print(f"\n❌ Missing Columns ({len(missing_columns)}):")
    for table, col, col_type in missing_columns:
        print(f"   - {table}.{col} ({col_type})")

if nullable_issues:
    print(f"\n⚠️  Nullable Issues ({len(nullable_issues)}):")
    for table, col in nullable_issues:
        print(f"   - {table}.{col} should be NOT NULL")

if data_quality_issues:
    print(f"\n⚠️  Data Quality Issues ({len(data_quality_issues)}):")
    for table, col, null_cnt, empty_cnt in data_quality_issues:
        print(f"   - {table}.{col}: {null_cnt} NULLs, {empty_cnt} empty strings")

print("\n" + "="*80)

if total_issues == 0:
    print("✅ ALL CRITICAL COLUMNS VERIFIED SUCCESSFULLY!")
    print("="*80 + "\n")
    sys.exit(0)
else:
    print(f"❌ FOUND {total_issues} ISSUE(S) - ACTION REQUIRED")
    print("="*80 + "\n")
    
    print("\n🔧 REMEDIATION STEPS:")
    if missing_tables:
        print("\n1. Create Missing Tables:")
        print("   Run the app.py initialization (it should auto-create tables)")
        print("   Or manually execute CREATE TABLE statements")
    
    if missing_columns:
        print("\n2. Add Missing Columns:")
        print("   Run: python -c \"from app import app; app.app_context().push()\"")
        print("   This will trigger _ensure_column() for all missing columns")
    
    if data_quality_issues:
        print("\n3. Fix Data Quality Issues:")
        print("   - Run update queries to populate NULL/empty values")
        print("   - For import_order: Run import_data_export_json.py")
        print("   - For subject_id: Run data migration scripts")
    
    sys.exit(1)
