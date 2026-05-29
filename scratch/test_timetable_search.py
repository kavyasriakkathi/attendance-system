"""Diagnose timetable search filtering issues."""
import sqlite3
import os

db_path = os.path.join(os.path.dirname(__file__), '..', 'attendance.db')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

def test_search(term):
    like = f"%{term}%"
    like_lower = f"%{term.lower()}%"
    # The CURRENT backend query (using %s placeholder - wrong for SQLite!)
    # SQLite needs ? not %s. That's bug #1.
    # Bug #2: no LOWER() normalization on te.section or b.name
    sql = """
      SELECT te.section, b.name as branch_name, s.name as subject_name,
             t.name as teacher_name, te.day, te.room
      FROM timetable_entries te
      LEFT JOIN subjects s ON te.subject_id = s.id
      LEFT JOIN teachers t ON te.teacher_id = t.id
      LEFT JOIN branches b ON te.branch_id = b.id
      WHERE (
        LOWER(COALESCE(s.name,'')) LIKE LOWER(?)
        OR LOWER(COALESCE(t.name,'')) LIKE LOWER(?)
        OR LOWER(COALESCE(te.section,'')) LIKE LOWER(?)
        OR LOWER(COALESCE(b.name,'')) LIKE LOWER(?)
        OR LOWER(COALESCE(te.room,'')) LIKE LOWER(?)
        OR LOWER(COALESCE(te.day,'')) LIKE LOWER(?)
      )
      LIMIT 10
    """
    rows = conn.execute(sql, (like, like, like, like, like, like)).fetchall()
    print(f"\nSearch: '{term}' → {len(rows)} rows")
    for r in rows[:5]:
        print(f"  branch={r['branch_name']} section={r['section']} subject={r['subject_name']} faculty={r['teacher_name']}")

# Test various searches
test_search("CSE-A")
test_search("CSM-A")
test_search("CSE-B")
test_search("CSE")
test_search("CSM")
test_search("Monday")
test_search("LAB")

# Also show the %s placeholder issue - this would break on SQLite
print("\n\n--- DIAGNOSIS ---")
print("Bug 1: Backend uses %s placeholder (PostgreSQL) but SQLite needs ?")
print("       get_placeholder() should return the right one but the WHERE clause")
print("       is built as a hardcoded string with %s markers.\n")

# Check what get_placeholder does
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
try:
    from app import get_placeholder
    ph = get_placeholder()
    print(f"Bug 1 check: get_placeholder() returns '{ph}'")
    if ph == '?':
        print("  -> SQLite mode. The WHERE clause in timetable.py hardcodes %s - MISMATCH!")
    else:
        print("  -> PostgreSQL mode. The WHERE clause uses %s - matches.")
except Exception as e:
    print(f"  Could not import get_placeholder: {e}")

conn.close()
