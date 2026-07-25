from pathlib import Path
import pytest
from werkzeug.security import generate_password_hash
import app as app_module

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("FLASK_ENV", "development")

    db_path = Path(tmp_path) / "test_student_auth.db"
    app_module.app.config.update(
        TESTING=True,
        DATABASE=str(db_path),
        SECRET_KEY="test-secret-key",
    )

    app_module._DB_INIT_DONE = False

    db = app_module.get_db()
    try:
        app_module.init_db(db)
        app_module._ensure_security_schema(db)

        db.execute("INSERT OR REPLACE INTO branches (id, name, location) VALUES (100, 'CSE', 'Building A')")
        
        # Student 1: With explicit user account in users table (uppercase enrollment)
        db.execute("INSERT OR REPLACE INTO students (id, name, enrollment, branch_id, email) VALUES (501, 'Student Uppercase', '21CSE101', 100, 'st101@test.com')")
        db.execute("INSERT OR REPLACE INTO users (id, username, password, role, student_id) VALUES (501, '21CSE101', ?, 'student', 501)", (generate_password_hash("E101"),))

        # Student 2: In students table ONLY (missing user account in users table)
        db.execute("INSERT OR REPLACE INTO students (id, name, enrollment, branch_id, email) VALUES (502, 'Student Missing User', '21CSE502', 100, 'st502@test.com')")

        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass

    with app_module.app.test_client() as c:
        yield c


def test_student_login_exact_case(client):
    """Test standard student login with exact username matching."""
    res = client.post('/student_login', data={
        'username': '21CSE101',
        'password': 'E101'
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'Student Portal' in res.data or b'Attendance' in res.data or b'Dashboard' in res.data


def test_student_login_case_insensitive(client):
    """Test student login with lowercase input when DB username is uppercase (PostgreSQL issue fix)."""
    res = client.post('/student_login', data={
        'username': '21cse101',
        'password': 'E101'
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'Invalid student login credentials' not in res.data


def test_student_login_auto_provisioning_missing_user(client):
    """Test login for a student that exists in students table but has no record in users table."""
    res = client.post('/student_login', data={
        'username': '21CSE502',
        'password': 'E502'
    }, follow_redirects=True)
    assert res.status_code == 200
    assert b'Invalid student login credentials' not in res.data
