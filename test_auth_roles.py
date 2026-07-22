import os
from pathlib import Path

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Ensure deterministic secret key for tests
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("FLASK_ENV", "development")

    import app as app_module

    db_path = Path(tmp_path) / "test.db"
    app_module.app.config.update(
        TESTING=True,
        DATABASE=str(db_path),
        SECRET_KEY="test-secret-key",
    )

    # Force init_db to run for this isolated DB
    app_module._DB_INIT_DONE = False

    # Initialize schema
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


def _seed_teacher(db, name="Teacher One", username="t1", password="pass123"):
    from werkzeug.security import generate_password_hash

    branch = db.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
    if not branch:
        db.execute("INSERT INTO branches (name, location) VALUES (?,?)", ("CSE", "Main"))
        db.commit()
        branch = db.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()

    subject = db.execute("SELECT id, name FROM subjects ORDER BY id LIMIT 1").fetchone()
    if not subject:
        db.execute("INSERT INTO subjects (name, branch_id) VALUES (?,?)", ("Mathematics", branch[0]))
        db.commit()
        subject = db.execute("SELECT id, name FROM subjects ORDER BY id LIMIT 1").fetchone()

    branch_id = branch[0] if branch else None
    subject_id = subject[0] if subject else None
    subject_name = subject[1] if subject else ""

    assert branch_id is not None
    assert subject_id is not None

    db.execute(
        "INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)",
        (username, generate_password_hash(password), "teacher"),
    )
    db.execute(
        "INSERT INTO teachers (name, username, password, subject_id, branch_id, subject_name) VALUES (?,?,?,?,?,?)",
        (name, username, generate_password_hash(password), subject_id, branch_id, subject_name),
    )
    teacher = db.execute("SELECT id FROM teachers WHERE username = ?", (username,)).fetchone()
    teacher_id = teacher[0]

    # Ensure junction tables have assignments
    db.execute(
        "INSERT OR IGNORE INTO teacher_branches (teacher_id, branch_id) VALUES (?,?)",
        (teacher_id, branch_id),
    )
    db.execute(
        "INSERT OR IGNORE INTO teacher_subjects (teacher_id, subject_id) VALUES (?,?)",
        (teacher_id, subject_id),
    )
    db.commit()

    return {
        "teacher_id": teacher_id,
        "username": username,
        "password": password,
    }


def test_admin_login_and_logout_flow(client):
    # Admin login
    resp = client.post("/login", data={"username": "admin", "password": "admin123"})
    assert resp.status_code in (302, 303)
    assert "/dashboard" in resp.headers.get("Location", "")

    # Dashboard accessible
    resp2 = client.get("/dashboard")
    assert resp2.status_code == 200

    # Logout clears session
    out = client.get("/logout")
    assert out.status_code in (302, 303)

    # Protected page now redirects
    resp3 = client.get("/dashboard")
    assert resp3.status_code in (302, 303)


def test_teacher_login_and_scope_blocking(client):
    import app as app_module

    db = app_module.get_db()
    try:
        teacher = _seed_teacher(db)
    finally:
        try:
            db.close()
        except Exception:
            pass

    # Teacher login
    resp = client.post(
        "/teacher_login",
        data={"username": teacher["username"], "password": teacher["password"]},
    )
    assert resp.status_code in (302, 303)

    # Teacher dashboard should load
    dash = client.get("/teacher/dashboard")
    assert dash.status_code == 200

    # Teacher cannot access admin settings page
    admin_att = client.get("/settings")
    assert admin_att.status_code in (302, 303)


def test_unauthorized_teacher_blocked_without_teacher_id(client):
    # Seed a malformed session (role teacher but missing teacher_id)
    with client.session_transaction() as sess:
        sess.clear()
        sess["user_id"] = 1
        sess["role"] = "teacher"

    resp = client.get("/teacher/dashboard")
    assert resp.status_code in (302, 303)
    assert "/teacher_login" in resp.headers.get("Location", "")
