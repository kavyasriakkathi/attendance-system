import importlib
import os
import sys


def build_test_app(tmp_path):
    db_path = str(tmp_path / "attendance_test.db")
    os.environ["DATABASE_URL"] = db_path
    sys.modules.pop("app", None)
    app_module = importlib.import_module("app")
    app_module.app.config.update(TESTING=True)
    return app_module.app


def test_teacher_management_and_assignment_flow(tmp_path):
    app = build_test_app(tmp_path)

    with app.test_client() as client:
        login_resp = client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=True,
        )
        assert login_resp.status_code == 200

        create_resp = client.post(
            "/teachers",
            data={
                "action": "add",
                "name": "Siddheshwar",
                "username": "siddheshwar",
                "password": "secret123",
                "email": "siddheshwar@example.com",
                "phone": "1234567890",
                "status": "active",
            },
            follow_redirects=True,
        )
        assert create_resp.status_code == 200
        assert b"Teacher created" in create_resp.data or b"Teacher created." in create_resp.data

        assign_resp = client.post(
            "/assign-teachers",
            data={
                "teacher_id": "1",
                "subject_id": "1",
                "branch_id": "1",
                "section": "A",
                "semester": "1",
                "academic_year": "2026-27",
            },
            follow_redirects=True,
        )
        assert assign_resp.status_code == 200
        assert b"Assignment saved" in assign_resp.data or b"Assignment saved." in assign_resp.data

        teachers_resp = client.get("/teachers")
        assert teachers_resp.status_code == 200
        assert b"Siddheshwar" in teachers_resp.data
