import sqlite3
import sys
import os

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import verify_database_schema, get_teacher_context
from timetable import ensure_timetable_tables, auto_setup_academic_from_slots, import_slots_streaming

def test_timetable_single_source_academic_setup():
    db_path = "scratch/test_timetable_academic.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Initialize app schema and timetable schema
    conn.execute("CREATE TABLE IF NOT EXISTS branches (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, code TEXT, branch_id INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS teachers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, username TEXT UNIQUE, password TEXT, password_hash TEXT, status TEXT, branch_id INTEGER, subject_id INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS teacher_branches (id INTEGER PRIMARY KEY AUTOINCREMENT, teacher_id INTEGER NOT NULL, branch_id INTEGER NOT NULL, UNIQUE(teacher_id, branch_id))")
    conn.execute("CREATE TABLE IF NOT EXISTS teacher_subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, teacher_id INTEGER NOT NULL, subject_id INTEGER NOT NULL, UNIQUE(teacher_id, subject_id))")
    conn.execute("CREATE TABLE IF NOT EXISTS teacher_subject_assignments (id INTEGER PRIMARY KEY AUTOINCREMENT, teacher_id INTEGER NOT NULL, subject_id INTEGER NOT NULL, branch_id INTEGER NOT NULL, section TEXT, semester TEXT, academic_year TEXT)")
    verify_database_schema(conn)
    ensure_timetable_tables(conn)

    # Define sample timetable slots extracted from a timetable file
    slots = [
        {
            "branch": "CSM",
            "section": "CSM-A",
            "semester": 1,
            "day": "Monday",
            "start_time": "09:00",
            "end_time": "10:00",
            "subject_name": "Data Structures",
            "sub_code": "DS",
            "faculty_name": "Dr. Alan Turing",
            "is_lab": 0,
            "room": "301"
        },
        {
            "branch": "CSM",
            "section": "CSM-A",
            "semester": 1,
            "day": "Monday",
            "start_time": "10:00",
            "end_time": "11:00",
            "subject_name": "Basic Electrical Engineering",
            "sub_code": "BEE",
            "faculty_name": "Prof. Nikola Tesla",
            "is_lab": 0,
            "room": "302"
        }
    ]

    print("--- 1. Testing auto_setup_academic_from_slots ---")
    summary = auto_setup_academic_from_slots(conn, slots)
    print("Auto setup summary:", summary)

    assert summary["subjects_created"] >= 2, f"Expected at least 2 subjects created, got {summary['subjects_created']}"
    assert summary["teachers_created"] >= 2, f"Expected at least 2 teachers created, got {summary['teachers_created']}"
    assert summary["assignments_created"] >= 2, f"Expected at least 2 assignments created, got {summary['assignments_created']}"

    # Verify Branches
    b_rows = conn.execute("SELECT * FROM branches").fetchall()
    print("Branches created:", [dict(b) for b in b_rows])
    assert len(b_rows) >= 1

    # Verify Subjects
    s_rows = conn.execute("SELECT * FROM subjects").fetchall()
    print("Subjects created:", [dict(s) for s in s_rows])
    assert len(s_rows) >= 2

    # Verify Teachers
    t_rows = conn.execute("SELECT * FROM teachers").fetchall()
    print("Teachers created:", [dict(t) for t in t_rows])
    assert len(t_rows) >= 2
    t1 = t_rows[0]
    assert t1["username"] is not None and len(t1["username"]) > 0

    # Verify Teacher Assignments
    assign_rows = conn.execute("SELECT * FROM teacher_subject_assignments").fetchall()
    print("Teacher Subject Assignments:", [dict(a) for a in assign_rows])
    assert len(assign_rows) >= 2

    print("--- 2. Testing import_slots_streaming ---")
    import_result = import_slots_streaming(conn, slots)
    print("Import result:", import_result)

    entries = conn.execute("SELECT * FROM timetable_entries").fetchall()
    print("Imported Timetable Entries count:", len(entries))
    assert len(entries) == 2

    print("--- 3. Testing Integration with Teacher Dashboard & Context ---")
    from app import app
    with app.test_request_context():
        from flask import session
        session["role"] = "teacher"
        session["teacher_id"] = t1["id"]
        context = get_teacher_context(conn)
        print("Teacher Context retrieved successfully for teacher ID", t1["id"], ":")
        print("Context keys:", list(context.keys()))
        print("Teacher Name:", context["teacher"]["name"])
        print("Assignments count:", len(context.get("assigned_classes", [])))
        print("Assigned Branches count:", len(context.get("assigned_branches", [])))
        print("Assigned Subjects count:", len(context.get("assigned_subjects", [])))

        assert len(context.get("assigned_classes", [])) > 0 or len(context.get("assigned_subjects", [])) > 0

    conn.close()
    if os.path.exists(db_path):
        os.remove(db_path)

    print("\nSUCCESS: All tests passed for Timetable Single-Source Academic Setup!")

if __name__ == "__main__":
    test_timetable_single_source_academic_setup()
