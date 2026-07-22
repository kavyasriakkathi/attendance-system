import sqlite3
import os
import sys
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import (
    app,
    get_db,
    _ensure_parent_schema,
    _ensure_student_profile_schema,
    _ensure_results_schema,
    generate_parent_alerts_for_student,
    create_parent_notification,
)

def test_parent_portal():
    print("--- Testing Parent Portal Module ---")
    db_path = os.path.join("scratch", "test_parent_portal.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Create base schemas
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

    conn.execute("INSERT INTO subjects (name, code, branch_id) VALUES ('Database Systems', 'DBMS', ?)", (branch_id,))
    s1_id = conn.execute("SELECT id FROM subjects WHERE code = 'DBMS'").fetchone()[0]

    conn.execute(
        "INSERT INTO students (name, enrollment, branch_id, email) VALUES ('David Miller', '21CSM002', ?, 'david@test.com')",
        (branch_id,)
    )
    student_id = conn.execute("SELECT id FROM students WHERE enrollment = '21CSM002'").fetchone()[0]

    # Ensure Parent Schema
    _ensure_parent_schema(conn)
    _ensure_student_profile_schema(conn)
    _ensure_results_schema(conn)

    # Verify parent table creation
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "parents" in tables, "parents table missing"
    assert "parent_notifications" in tables, "parent_notifications table missing"
    assert "college_announcements" in tables, "college_announcements table missing"

    # Register parent account
    hashed_pw = generate_password_hash("parent123")
    conn.execute(
        "INSERT INTO parents (name, username, password, phone, email, student_id) VALUES ('John Miller', 'parent_john', ?, '9876543210', 'john@test.com', ?)",
        (hashed_pw, student_id)
    )
    conn.commit()

    # Seed low attendance (2 present, 4 absent = 33.3% -> Shortage Warning!)
    for i in range(2):
        conn.execute("INSERT INTO attendance (student_id, branch_id, subject_id, subject_name, date, status) VALUES (?, ?, ?, 'Database Systems', '2026-07-01', 'Present')", (student_id, branch_id, s1_id))
    for i in range(4):
        conn.execute("INSERT INTO attendance (student_id, branch_id, subject_id, subject_name, date, status) VALUES (?, ?, ?, 'Database Systems', '2026-07-02', 'Absent')", (student_id, branch_id, s1_id))

    # Seed exam results
    conn.execute("INSERT INTO exams (exam_name, exam_type, academic_year, semester, branch_id, section) VALUES ('Internal-1', 'Internal', '2025-2026', 'Semester 1', ?, 'CSM-A')", (branch_id,))
    exam_id = conn.execute("SELECT id FROM exams WHERE exam_name = 'Internal-1'").fetchone()[0]

    conn.execute("INSERT INTO marks (student_id, subject_id, exam_id, mid1_marks, external_marks, marks_obtained, max_marks, entered_by_teacher) VALUES (?, ?, ?, 22.0, 55.0, 77.0, 100.0, 1)", (student_id, s1_id, exam_id))
    conn.commit()

    # Generate Alerts
    generate_parent_alerts_for_student(conn, student_id)

    # Check generated notifications
    notifs = conn.execute("SELECT * FROM parent_notifications WHERE student_id = ?", (student_id,)).fetchall()
    print("Generated Parent Notifications:")
    for n in notifs:
        print(f"  [{n['type']}] {n['title']}: {n['message']}")

    assert len(notifs) >= 1, "Parent alerts were not generated"
    has_shortage_notif = any(n['type'] == 'attendance_shortage' for n in notifs)
    assert has_shortage_notif is True, "Attendance shortage notification missing"

    # Test Announcement insertion
    conn.execute("INSERT INTO college_announcements (title, content, target_audience, created_by) VALUES ('Parent Teacher Meet', 'Annual PTM on July 30th at 10 AM', 'parents', 'Admin')")
    conn.commit()

    announcements = conn.execute("SELECT * FROM college_announcements").fetchall()
    assert len(announcements) == 1, "Announcement creation failed"
    print("Announcement:", announcements[0]["title"], "-", announcements[0]["content"])

    conn.close()
    if os.path.exists(db_path):
        os.remove(db_path)

    print("SUCCESS: Parent Portal Module test passed!")

if __name__ == "__main__":
    test_parent_portal()
