import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("FLASK_ENV", "development")

from flask import session

import app as app_module
from app import app, get_db, get_teacher_context, row_get

from werkzeug.security import generate_password_hash


def _select_teacher_with_assignments():
    db = get_db()
    try:
        teacher = db.execute(
            """
            SELECT t.id, t.name, t.username, t.subject_id,
                   COUNT(DISTINCT tb.branch_id) AS branch_count,
                   COUNT(DISTINCT ts.subject_id) AS subject_count
            FROM teachers t
            LEFT JOIN teacher_branches tb ON tb.teacher_id = t.id
            LEFT JOIN teacher_subjects ts ON ts.teacher_id = t.id
            GROUP BY t.id, t.name, t.username, t.subject_id
            HAVING COUNT(DISTINCT tb.branch_id) > 0
               AND COUNT(DISTINCT ts.subject_id) > 0
            ORDER BY t.id
            LIMIT 1
            """
        ).fetchone()
        return teacher
    finally:
        try:
            db.close()
        except Exception:
            pass


class TeacherContextSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use an isolated DB so this test never depends on (or mutates) the real app DB.
        cls._tmpdir = tempfile.mkdtemp(prefix="teacher-context-")
        db_path = Path(cls._tmpdir) / "teacher_context_test.db"
        app.config.update(
            TESTING=True,
            DATABASE=str(db_path),
            SECRET_KEY=os.environ.get("SECRET_KEY", "test-secret-key"),
        )
        app_module._DB_INIT_DONE = False

        db = get_db()
        try:
            branch = db.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
            subject = db.execute("SELECT id, name FROM subjects ORDER BY id LIMIT 1").fetchone()
            if not branch or not subject:
                raise AssertionError("Branches/subjects did not initialize correctly")

            db.execute(
                "INSERT INTO teachers (name, username, password, subject_id, branch_id, subject_name) VALUES (?,?,?,?,?,?)",
                (
                    "Test Teacher",
                    "teacher_smoke",
                    generate_password_hash("pass123"),
                    row_get(subject, "id"),
                    row_get(branch, "id"),
                    row_get(subject, "name"),
                ),
            )
            teacher_id = db.execute(
                "SELECT id FROM teachers WHERE username = ?",
                ("teacher_smoke",),
            ).fetchone()[0]

            db.execute(
                "INSERT INTO teacher_branches (teacher_id, branch_id) VALUES (?,?)",
                (teacher_id, row_get(branch, "id")),
            )
            db.execute(
                "INSERT INTO teacher_subjects (teacher_id, subject_id) VALUES (?,?)",
                (teacher_id, row_get(subject, "id")),
            )
            db.commit()
        finally:
            try:
                db.close()
            except Exception:
                pass

    @classmethod
    def tearDownClass(cls):
        try:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)
        except Exception:
            pass

    def setUp(self):
        self.teacher = _select_teacher_with_assignments()
        if not self.teacher:
            self.fail("No teacher found with both branch and subject assignments.")

        self.teacher_id = row_get(self.teacher, "id")
        self.teacher_name = row_get(self.teacher, "name")

        db = get_db()
        try:
            self.branch = db.execute(
                "SELECT branch_id FROM teacher_branches WHERE teacher_id = ? ORDER BY branch_id LIMIT 1",
                (self.teacher_id,),
            ).fetchone()
            self.subject = db.execute(
                "SELECT subject_id FROM teacher_subjects WHERE teacher_id = ? ORDER BY subject_id LIMIT 1",
                (self.teacher_id,),
            ).fetchone()
        finally:
            try:
                db.close()
            except Exception:
                pass

        if not self.branch or not self.subject:
            self.fail("Selected teacher is missing a branch or subject assignment.")

    def _seed_teacher_session(self, client, include_teacher_id=True):
        with client.session_transaction() as sess:
            sess["user_id"] = self.teacher_id
            sess["role"] = "teacher"
            if include_teacher_id:
                sess["teacher_id"] = self.teacher_id
            sess["teacher_name"] = self.teacher_name
            sess["teacher_branch_id"] = row_get(self.branch, "branch_id")
            sess["teacher_subject_id"] = row_get(self.subject, "subject_id")

    def test_get_teacher_context_loads_assignments(self):
        with app.test_request_context("/teacher/dashboard"):
            session["user_id"] = self.teacher_id
            session["role"] = "teacher"
            session["teacher_id"] = self.teacher_id
            session["teacher_name"] = self.teacher_name
            session["teacher_branch_id"] = row_get(self.branch, "branch_id")
            session["teacher_subject_id"] = row_get(self.subject, "subject_id")

            db = get_db()
            try:
                ctx = get_teacher_context(db)
            finally:
                try:
                    db.close()
                except Exception:
                    pass

            self.assertIsNotNone(ctx)
            self.assertIsNotNone(ctx["teacher"])
            self.assertIsNotNone(ctx["subject_row"])
            self.assertIsNotNone(ctx["current_branch_id"])
            self.assertGreaterEqual(ctx["assigned_branches_count"], 1)
            self.assertGreaterEqual(ctx["assigned_subjects_count"], 1)
            self.assertIsInstance(ctx["assigned_branches"], list)
            self.assertIsInstance(ctx["assigned_subjects"], list)

    def test_teacher_dashboard_loads(self):
        with app.test_client() as client:
            self._seed_teacher_session(client)
            resp = client.get("/teacher/dashboard")
            self.assertEqual(resp.status_code, 200)
            body = resp.data.decode("utf-8", errors="ignore")
            self.assertTrue("teacher" in body.lower() or "dashboard" in body.lower())

    def test_unauthorized_teacher_blocked(self):
        with app.test_client() as client:
            self._seed_teacher_session(client, include_teacher_id=False)
            resp = client.get("/teacher/dashboard")
            self.assertIn(resp.status_code, (302, 303))
            self.assertIn("/teacher_login", resp.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
