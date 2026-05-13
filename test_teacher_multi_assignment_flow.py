import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("FLASK_ENV", "development")

import app as app_module
from app import app, get_db, row_get


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("FLASK_ENV", "development")

    db_path = Path(tmp_path) / "test.db"
    app_module.app.config.update(
        TESTING=True,
        DATABASE=str(db_path),
        SECRET_KEY="test-secret-key",
    )
    app_module._DB_INIT_DONE = False

    db = app_module.get_db()
    try:
        pass
    finally:
        try:
            db.close()
        except Exception:
            pass

    with app_module.app.test_client() as c:
        yield c


def _admin_session(client):
    with client.session_transaction() as sess:
        sess.clear()
        sess["user_id"] = 1
        sess["username"] = "admin"
        sess["role"] = "admin"


def _get_rows(db, query, params=()):
    rows = db.execute(query, params).fetchall()
    return rows or []


def test_teacher_multi_subject_branch_flow(client):
    db = get_db()
    try:
        branches = _get_rows(db, "SELECT id, name FROM branches ORDER BY id LIMIT 2")
        subjects = _get_rows(db, "SELECT id, name FROM subjects ORDER BY id LIMIT 2")

        while len(branches) < 2:
            branch_name = f"TEMP_BRANCH_{uuid.uuid4().hex[:6]}"
            db.execute(
                "INSERT INTO branches (name, location) VALUES (?, ?)",
                (branch_name, "Temp"),
            )
            db.commit()
            branches = _get_rows(db, "SELECT id, name FROM branches ORDER BY id LIMIT 2")

        while len(subjects) < 2:
            subject_name = f"TEMP_SUBJECT_{uuid.uuid4().hex[:6]}"
            branch_for_subject = row_get(branches[0], "id")
            db.execute(
                "INSERT INTO subjects (name, branch_id) VALUES (?, ?)",
                (subject_name, branch_for_subject),
            )
            db.commit()
            subjects = _get_rows(db, "SELECT id, name FROM subjects ORDER BY id LIMIT 2")

        teacher_username = f"multi_{uuid.uuid4().hex[:8]}"
        teacher_name = "Multi Assignment Teacher"
        password = "pass123"

        _admin_session(client)
        add_resp = client.post(
            "/admin/teachers",
            data={
                "action": "add",
                "name": teacher_name,
                "username": teacher_username,
                "password": password,
                "subject_ids": [str(row_get(subjects[0], "id")), str(row_get(subjects[1], "id"))],
                "branch_ids": [str(row_get(branches[0], "id")), str(row_get(branches[1], "id"))],
            },
            follow_redirects=True,
        )
        assert add_resp.status_code == 200
        body = add_resp.data.decode("utf-8", errors="ignore")
        assert teacher_name in body

        teacher_row = db.execute(
            "SELECT id FROM teachers WHERE username = ?",
            (teacher_username,),
        ).fetchone()
        assert teacher_row is not None
        teacher_id = row_get(teacher_row, "id")

        subject_rows = db.execute(
            "SELECT s.id, s.name FROM subjects s JOIN teacher_subjects ts ON ts.subject_id = s.id WHERE ts.teacher_id = ? ORDER BY s.id",
            (teacher_id,),
        ).fetchall()
        branch_rows = db.execute(
            "SELECT b.id, b.name FROM branches b JOIN teacher_branches tb ON tb.branch_id = b.id WHERE tb.teacher_id = ? ORDER BY b.id",
            (teacher_id,),
        ).fetchall()
        assert len(subject_rows) == 2
        assert len(branch_rows) == 2

        client.get("/logout")
        login_resp = client.post(
            "/teacher_login",
            data={"username": teacher_username, "password": password},
            follow_redirects=False,
        )
        assert login_resp.status_code in (302, 303)

        dash_resp = client.get("/teacher/dashboard")
        assert dash_resp.status_code == 200
        dash_body = dash_resp.data.decode("utf-8", errors="ignore")
        assert row_get(subject_rows[0], "name") in dash_body
        assert row_get(subject_rows[1], "name") in dash_body
        assert row_get(branch_rows[0], "name") in dash_body
        assert row_get(branch_rows[1], "name") in dash_body

        student_row = db.execute(
            "SELECT id, name, enrollment FROM students WHERE branch_id = ? ORDER BY id LIMIT 1",
            (row_get(branch_rows[0], "id"),),
        ).fetchone()
        if student_row is None:
            enrollment = f"ENR_{uuid.uuid4().hex[:8]}"
            student_name = f"Student {uuid.uuid4().hex[:4]}"
            db.execute(
                "INSERT INTO students (name, enrollment, email, branch_id) VALUES (?, ?, ?, ?)",
                (student_name, enrollment, f"{enrollment.lower()}@example.com", row_get(branch_rows[0], "id")),
            )
            db.commit()
            student_row = db.execute(
                "SELECT id, name, enrollment FROM students WHERE branch_id = ? ORDER BY id DESC LIMIT 1",
                (row_get(branch_rows[0], "id"),),
            ).fetchone()
        assert student_row is not None

        attend_resp = client.get(
            "/teacher/attendance",
            query_string={
                "subject_id": row_get(subject_rows[0], "id"),
                "branch_id": row_get(branch_rows[0], "id"),
                "date": "2026-05-13",
                "period": "1",
            },
        )
        assert attend_resp.status_code == 200
        attend_body = attend_resp.data.decode("utf-8", errors="ignore")
        assert row_get(student_row, "name") in attend_body

        save_resp = client.post(
            "/teacher/attendance",
            data={
                "date": "2026-05-13",
                "period": "1",
                "subject_id": row_get(subject_rows[0], "id"),
                "branch_id": row_get(branch_rows[0], "id"),
                "student_id": row_get(student_row, "id"),
                f"status_{row_get(student_row, 'id')}": "Present",
                f"note_{row_get(student_row, 'id')}": "Verified",
            },
            follow_redirects=True,
        )
        assert save_resp.status_code == 200

        saved_row = db.execute(
            """
            SELECT id, status, note, branch_section
            FROM attendance
            WHERE student_id = ? AND branch_id = ? AND subject_id = ? AND date = ? AND period = ?
            """,
            (
                row_get(student_row, "id"),
                row_get(branch_rows[0], "id"),
                row_get(subject_rows[0], "id"),
                "2026-05-13",
                "1",
            ),
        ).fetchone()
        assert saved_row is not None
        assert row_get(saved_row, "status") == "Present"
        assert row_get(saved_row, "branch_section") == row_get(branch_rows[0], "name")

        _admin_session(client)
        unassigned_name = f"UNASSIGNED_{uuid.uuid4().hex[:6]}"
        create_branch = client.post(
            "/branches",
            data={"action": "add", "name": unassigned_name, "location": "Temp", "sections": ""},
            follow_redirects=True,
        )
        assert create_branch.status_code == 200
        unassigned_row = db.execute(
            "SELECT id FROM branches WHERE name = ?",
            (unassigned_name,),
        ).fetchone()
        assert unassigned_row is not None
        unassigned_branch_id = row_get(unassigned_row, "id")

        client.get("/logout")
        client.post(
            "/teacher_login",
            data={"username": teacher_username, "password": password},
            follow_redirects=False,
        )
        blocked_resp = client.get(
            "/teacher/attendance",
            query_string={
                "subject_id": row_get(subject_rows[0], "id"),
                "branch_id": unassigned_branch_id,
                "date": "2026-05-13",
                "period": "1",
            },
            follow_redirects=False,
        )
        assert blocked_resp.status_code in (302, 403)
    finally:
        try:
            db.close()
        except Exception:
            pass
