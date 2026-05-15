import os
import json
from datetime import datetime, timedelta

from app import app, get_db


TEST_DB = os.path.join(os.path.dirname(__file__), "validate_test.db")


def ensure_test_db():
    if os.path.exists(TEST_DB):
        try:
            os.remove(TEST_DB)
        except Exception:
            pass
    app.config["DATABASE"] = TEST_DB


def run_checks():
    ensure_test_db()
    results = {}
    # initialize DB and routes
    with app.app_context():
        db = get_db()
        # basic health
    # Use test client to call endpoints and manage session
    client = app.test_client()

    # Health as admin
    with client.session_transaction() as sess:
        sess["role"] = "admin"
        sess["user_id"] = 1

    r = client.get("/health/timetable")
    try:
        results['health'] = r.get_json()
    except Exception:
        results['health'] = {"status_code": r.status_code, "data": r.data.decode('utf-8')}

    # Now populate sample data to test active slot and attendance
    with app.app_context():
        db = get_db()
        placeholder = "%s" if str(app.config.get("DATABASE", "")).startswith("postgres") else "?"
        # Create branch, subject, teacher, student
        cur = db.execute("INSERT INTO branches (name, location) VALUES (%s, %s)".replace("%s", placeholder), ("TestBranch", "Campus"))
        branch_id = cur.lastrowid if hasattr(cur, 'lastrowid') else None
        if branch_id is None:
            row = db.execute(f"SELECT id FROM branches WHERE name = {placeholder}", ("TestBranch",)).fetchone()
            branch_id = row['id'] if row else None

        db.execute(f"INSERT INTO subjects (name, branch_id) VALUES ({placeholder}, {placeholder})", ("TestSubject", branch_id))
        sub = db.execute(f"SELECT id FROM subjects WHERE name = {placeholder}", ("TestSubject",)).fetchone()
        subject_id = sub['id']

        db.execute(f"INSERT INTO teachers (name, username, password, branch_id, subject_name) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})", ("Teacher One", "t1", "x", branch_id, "TestSubject"))
        trow = db.execute(f"SELECT id FROM teachers WHERE username = {placeholder}", ("t1",)).fetchone()
        teacher_id = trow['id']

        # Create a student
        db.execute(f"INSERT INTO students (name, enrollment, branch_id, section) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})", ("Student A", "ENR1", branch_id, "A"))
        srow = db.execute(f"SELECT id FROM students WHERE enrollment = {placeholder}", ("ENR1",)).fetchone()
        student_id = srow['id']

        # Insert a timetable_entries row for current time window
        now = datetime.now()
        st = (now - timedelta(minutes=10)).strftime("%H:%M")
        et = (now + timedelta(minutes=10)).strftime("%H:%M")
        weekday = now.strftime("%A")
        db.execute(f"INSERT INTO timetable_entries (branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, is_lab, room) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})", (branch_id, "A", 1, weekday, st, et, subject_id, teacher_id, 0, "R1"))
        db.commit()

    # Call /timetable/active as teacher
    with client.session_transaction() as sess:
        sess["role"] = "teacher"
        sess["user_id"] = teacher_id
        sess["teacher_id"] = teacher_id
        sess["teacher_branch_name"] = "TestBranch"
        sess["teacher_section"] = "A"

    r2 = client.get("/timetable/active")
    results['timetable_active'] = r2.get_json()

    # Mark attendance bulk_absent
    r3 = client.post("/attendance/mark_current", json={"action": "bulk_absent"})
    try:
        results['mark_bulk_absent'] = r3.get_json()
    except Exception:
        results['mark_bulk_absent'] = {"status_code": r3.status_code, "data": r3.data.decode('utf-8')}

    # Check attendance counts
    with app.app_context():
        db = get_db()
        arow = db.execute("SELECT COUNT(1) AS c FROM attendance").fetchone()
        results['attendance_count'] = int(arow[0] if arow is not None else 0)
        srow = db.execute("SELECT COUNT(1) AS c FROM timetable_entries").fetchone()
        results['timetable_entries_count'] = int(srow[0] if srow is not None else 0)

    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    run_checks()
