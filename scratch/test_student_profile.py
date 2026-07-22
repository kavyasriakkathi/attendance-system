import sqlite3
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import (
    app,
    get_db,
    _ensure_student_profile_schema,
    get_student_academic_profile_context,
    _ensure_results_schema,
    calculate_grade
)

def test_student_academic_profile():
    print("--- Testing Student Academic Profile Module ---")
    db_path = os.path.join("scratch", "test_student_profile.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Create necessary base tables
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT,
            branch_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enrollment TEXT UNIQUE NOT NULL,
            branch_id INTEGER NOT NULL,
            email TEXT
        );
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            subject_name TEXT,
            date TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            student_id INTEGER
        );
    """)

    # Seed data
    conn.execute("INSERT INTO branches (name) VALUES ('CSM')")
    branch_id = conn.execute("SELECT id FROM branches WHERE name = 'CSM'").fetchone()[0]

    conn.execute("INSERT INTO subjects (name, code, branch_id) VALUES ('Data Structures', 'DS', ?)", (branch_id,))
    conn.execute("INSERT INTO subjects (name, code, branch_id) VALUES ('Operating Systems', 'OS', ?)", (branch_id,))
    s1_id = conn.execute("SELECT id FROM subjects WHERE code = 'DS'").fetchone()[0]
    s2_id = conn.execute("SELECT id FROM subjects WHERE code = 'OS'").fetchone()[0]

    conn.execute(
        "INSERT INTO students (name, enrollment, branch_id, email) VALUES ('Alice Smith', '21CSM001', ?, 'alice@test.com')",
        (branch_id,)
    )
    student_id = conn.execute("SELECT id FROM students WHERE enrollment = '21CSM001'").fetchone()[0]

    # Ensure Profile Schema
    _ensure_student_profile_schema(conn)

    # Verify column creation
    cols = [r[1] for r in conn.execute("PRAGMA table_info(students)").fetchall()]
    assert "section" in cols, "section column missing"
    assert "semester" in cols, "semester column missing"
    assert "status" in cols, "status column missing"

    # Seed attendance logs: DS (4 present out of 5 = 80%), OS (2 present out of 4 = 50% -> shortage!)
    for i in range(4):
        conn.execute("INSERT INTO attendance (student_id, branch_id, subject_id, subject_name, date, status) VALUES (?, ?, ?, 'Data Structures', '2026-07-01', 'Present')", (student_id, branch_id, s1_id))
    conn.execute("INSERT INTO attendance (student_id, branch_id, subject_id, subject_name, date, status) VALUES (?, ?, ?, 'Data Structures', '2026-07-02', 'Absent')", (student_id, branch_id, s1_id))

    for i in range(2):
        conn.execute("INSERT INTO attendance (student_id, branch_id, subject_id, subject_name, date, status) VALUES (?, ?, ?, 'Operating Systems', '2026-07-01', 'Present')", (student_id, branch_id, s2_id))
    for i in range(2):
        conn.execute("INSERT INTO attendance (student_id, branch_id, subject_id, subject_name, date, status) VALUES (?, ?, ?, 'Operating Systems', '2026-07-02', 'Absent')", (student_id, branch_id, s2_id))

    # Seed Exam Results
    _ensure_results_schema(conn)
    conn.execute("INSERT INTO exams (exam_name, exam_type, academic_year, semester, branch_id, section) VALUES ('Internal-1', 'Internal', '2025-2026', 'Semester 1', ?, 'CSM-A')", (branch_id,))
    exam_id = conn.execute("SELECT id FROM exams WHERE exam_name = 'Internal-1'").fetchone()[0]

    conn.execute("INSERT INTO marks (student_id, subject_id, exam_id, mid1_marks, external_marks, marks_obtained, max_marks, entered_by_teacher) VALUES (?, ?, ?, 25.0, 60.0, 85.0, 100.0, 1)", (student_id, s1_id, exam_id))
    conn.execute("INSERT INTO marks (student_id, subject_id, exam_id, mid1_marks, external_marks, marks_obtained, max_marks, entered_by_teacher) VALUES (?, ?, ?, 20.0, 50.0, 70.0, 100.0, 1)", (student_id, s2_id, exam_id))

    conn.commit()

    # Retrieve Context
    context = get_student_academic_profile_context(conn, student_id)
    print("Student Profile Loaded:")
    print("  Name:", context["student"]["name"])
    print("  Roll No:", context["student"]["enrollment"])
    print("  Branch:", context["student"]["branch_name"])
    print("  Section:", context["student"]["section"])
    print("  Semester:", context["student"]["semester"])
    print("  Overall Attendance %:", context["overall_attendance_pct"])
    print("  Attendance Shortage Warning:", context["attendance_shortage_warning"])
    print("  Shortage Subjects Count:", len(context["shortage_subjects"]))
    print("  CGPA:", context["cgpa"])
    print("  Performance Rating:", context["performance_rating"])
    print("  Promotion Status:", context["overall_promotion_status"])

    assert context["student"]["name"] == "Alice Smith"
    assert context["overall_attendance_pct"] > 0
    assert context["attendance_shortage_warning"] is True, "Operating Systems has 50% attendance (< 75%), should trigger shortage warning"
    assert context["cgpa"] > 0
    assert context["semesters_data"]["Semester 1"]["has_results"] is True

    conn.close()
    if os.path.exists(db_path):
        os.remove(db_path)

    print("SUCCESS: Student Academic Profile test passed!")

if __name__ == "__main__":
    test_student_academic_profile()
