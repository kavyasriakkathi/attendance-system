import os
import unittest
import uuid

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("FLASK_ENV", "development")

from app import app, get_db, get_placeholder, row_get


class BranchFlowTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

        self.base_name = f"COPILOT_BRANCH_{uuid.uuid4().hex[:8]}"
        self.section_a_name = f"{self.base_name}-A"
        self.section_b_name = f"{self.base_name}-B"
        self.updated_name = f"{self.base_name}-A1"
        self.location = f"Loc-{uuid.uuid4().hex[:6]}"

    def _fetch_branch_by_name(self, branch_name):
        db = get_db()
        placeholder = get_placeholder()
        try:
            return db.execute(
                f"SELECT id, name, location FROM branches WHERE name = {placeholder}",
                (branch_name,),
            ).fetchone()
        finally:
            try:
                db.close()
            except Exception:
                pass

    def _fetch_branch_by_id(self, branch_id):
        db = get_db()
        placeholder = get_placeholder()
        try:
            return db.execute(
                f"SELECT id, name, location FROM branches WHERE id = {placeholder}",
                (branch_id,),
            ).fetchone()
        finally:
            try:
                db.close()
            except Exception:
                pass

    def _count_named_branch(self, branch_name):
        db = get_db()
        placeholder = get_placeholder()
        try:
            row = db.execute(
                f"SELECT COUNT(*) AS count FROM branches WHERE name = {placeholder}",
                (branch_name,),
            ).fetchone()
            return row_get(row, "count", 0) or 0
        finally:
            try:
                db.close()
            except Exception:
                pass

    def test_add_edit_delete_branch_flow(self):
        created_branch_id = None

        try:
            login_response = self.client.post(
                "/login",
                data={"username": "admin", "password": "admin123"},
                follow_redirects=True,
            )
            self.assertEqual(login_response.status_code, 200)

            add_response = self.client.post(
                "/branches",
                data={
                    "action": "add",
                    "name": self.base_name,
                    "location": self.location,
                    "sections": "A,B",
                },
                follow_redirects=True,
            )
            self.assertEqual(add_response.status_code, 200)
            add_body = add_response.data.decode("utf-8", errors="ignore")
            self.assertIn(self.section_a_name, add_body)
            self.assertIn(self.section_b_name, add_body)

            created_a = self._fetch_branch_by_name(self.section_a_name)
            created_b = self._fetch_branch_by_name(self.section_b_name)
            self.assertIsNotNone(created_a)
            self.assertIsNotNone(created_b)
            created_branch_id = row_get(created_a, "id")
            self.assertEqual(row_get(created_a, "location"), self.location)
            self.assertEqual(row_get(created_b, "location"), self.location)

            duplicate_response = self.client.post(
                "/branches",
                data={
                    "action": "add",
                    "name": self.base_name,
                    "location": self.location,
                    "sections": "A,B",
                },
                follow_redirects=True,
            )
            self.assertEqual(duplicate_response.status_code, 200)
            self.assertEqual(self._count_named_branch(self.section_a_name), 1)
            self.assertEqual(self._count_named_branch(self.section_b_name), 1)

            edit_response = self.client.post(
                "/branches",
                data={
                    "action": "edit",
                    "branch_id": created_branch_id,
                    "name": self.updated_name,
                    "location": self.location,
                    "sections": "",
                },
                follow_redirects=True,
            )
            self.assertEqual(edit_response.status_code, 200)
            edit_body = edit_response.data.decode("utf-8", errors="ignore")
            self.assertIn(self.updated_name, edit_body)

            updated = self._fetch_branch_by_id(created_branch_id)
            self.assertIsNotNone(updated)
            self.assertEqual(row_get(updated, "name"), self.updated_name)
            self.assertEqual(row_get(updated, "location"), self.location)

            delete_response = self.client.post(
                "/branches",
                data={
                    "action": "delete",
                    "branch_id": created_branch_id,
                },
                follow_redirects=True,
            )
            self.assertEqual(delete_response.status_code, 200)
            delete_body = delete_response.data.decode("utf-8", errors="ignore")
            self.assertNotIn(self.updated_name, delete_body)

            db_after_delete = get_db()
            placeholder = get_placeholder()
            try:
                deleted_count = db_after_delete.execute(
                    f"SELECT COUNT(*) AS count FROM branches WHERE id = {placeholder}",
                    (created_branch_id,),
                ).fetchone()
            finally:
                try:
                    db_after_delete.close()
                except Exception:
                    pass

            self.assertEqual(row_get(deleted_count, "count", 0), 0)
        finally:
            cleanup_db = get_db()
            placeholder = get_placeholder()
            try:
                for branch_name in [self.section_a_name, self.section_b_name, self.updated_name]:
                    row = cleanup_db.execute(
                        f"SELECT id FROM branches WHERE name = {placeholder}",
                        (branch_name,),
                    ).fetchone()
                    if row:
                        cleanup_db.execute(
                            f"DELETE FROM branches WHERE id = {placeholder}",
                            (row_get(row, "id"),),
                        )
                cleanup_db.commit()
            except Exception:
                try:
                    cleanup_db.rollback()
                except Exception:
                    pass
            finally:
                try:
                    cleanup_db.close()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)