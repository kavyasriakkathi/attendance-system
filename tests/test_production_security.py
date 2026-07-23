"""
Production Security & Role Authorization Tests
Senior QA Architect & ERP Technical Lead
"""

import os
import sys
import pytest
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, _ensure_database_indexes


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key"
    with app.test_client() as client:
        yield client


@pytest.fixture
def test_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, role TEXT)")
    db.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, branch_id INTEGER)")
    db.execute("CREATE TABLE student_fee_assignments (id INTEGER PRIMARY KEY, student_id INTEGER, fee_structure_id INTEGER)")
    db.execute("CREATE TABLE fee_payments (id INTEGER PRIMARY KEY, assignment_id INTEGER, student_id INTEGER)")
    db.execute("CREATE TABLE attendance (id INTEGER PRIMARY KEY, student_id INTEGER, subject_id INTEGER, date TEXT)")
    db.execute("CREATE TABLE marks (id INTEGER PRIMARY KEY, student_id INTEGER, subject_id INTEGER)")
    db.execute("CREATE TABLE timetable_entries (id INTEGER PRIMARY KEY, branch_id INTEGER, day TEXT)")
    db.execute("CREATE TABLE teacher_subject_assignments (id INTEGER PRIMARY KEY, teacher_id INTEGER, branch_id INTEGER)")
    db.commit()
    yield db
    db.close()


def test_ensure_database_indexes(test_db):
    """Verify that secondary indexes are safely created on foreign key columns."""
    _ensure_database_indexes(test_db)
    indexes = test_db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    index_names = [r["name"] for r in indexes]

    assert "idx_sfa_student_id" in index_names
    assert "idx_fp_assignment_id" in index_names
    assert "idx_attendance_student_date" in index_names
    assert "idx_marks_student_subject" in index_names
    assert "idx_timetable_branch_day" in index_names


def test_unauthenticated_access_redirection(client):
    """Verify unauthenticated requests to protected endpoints redirect to login."""
    res = client.get("/admin/security", follow_redirects=False)
    assert res.status_code in (302, 401)
    assert "/admin-login" in res.headers.get("Location", "") or "/login" in res.headers.get("Location", "")


def test_student_idor_protection(client):
    """Verify student cannot access another student's dashboard."""
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["role"] = "student"
        sess["student_id"] = 101

    res = client.get("/student_dashboard/102", follow_redirects=False)
    assert res.status_code == 302
    assert "/student_dashboard" in res.headers.get("Location", "")
