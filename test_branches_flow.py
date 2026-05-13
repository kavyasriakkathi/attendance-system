import os
import unittest
import uuid

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("FLASK_ENV", "development")

from app import app, get_db, get_placeholder, row_get


class BranchFlowTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["role"] = "admin"

        self.base_name = f"COPILOT_BRANCH_{uuid.uuid4().hex[:8]}"
        self.updated_name = f"{self.base_name} Section B"
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

    def test_add_edit_delete_branch_flow(self):
        created_branch_id = None

        try:
            add_response = self.client.post(
                "/branches",
                data={
                    "action": "add",
                    "name": self.base_name,
                    "location": self.location,
                    "sections": "",
                },
                follow_redirects=True,
            )
            self.assertEqual(add_response.status_code, 200)
            add_body = add_response.data.decode("utf-8", errors="ignore")
            self.assertIn(self.base_name, add_body)

            created = self._fetch_branch_by_name(self.base_name)
            self.assertIsNotNone(created)
            created_branch_id = row_get(created, "id")
            self.assertEqual(row_get(created, "name"), self.base_name)
            self.assertEqual(row_get(created, "location"), self.location)

            edit_response = self.client.post(
                "/branches",
                data={
                    "action": "edit",
                    "branch_id": created_branch_id,
                    "name": self.base_name,
                    "location": self.location,
                    "sections": "Section B",
                },
                follow_redirects=True,
            )
            self.assertEqual(edit_response.status_code, 200)
            edit_body = edit_response.data.decode("utf-8", errors="ignore")
            self.assertIn(self.updated_name, edit_body)
            self.assertNotIn(f">{self.base_name}<", edit_body)

            updated = self._fetch_branch_by_id(created_branch_id)
            self.assertIsNotNone(updated)
            self.assertEqual(row_get(updated, "name"), self.updated_name)
            self.assertEqual(row_get(updated, "location"), self.location)

            updated_count_db = get_db()
            placeholder = get_placeholder()
            try:
                updated_count = updated_count_db.execute(
                    f"SELECT COUNT(*) AS count FROM branches WHERE name = {placeholder}",
                    (self.updated_name,),
                ).fetchone()
                original_count = updated_count_db.execute(
                    f"SELECT COUNT(*) AS count FROM branches WHERE name = {placeholder}",
                    (self.base_name,),
                ).fetchone()
            finally:
                try:
                    updated_count_db.close()
                except Exception:
                    pass

            self.assertEqual(row_get(updated_count, "count", 0), 1)
            self.assertEqual(row_get(original_count, "count", 0), 0)

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
            if created_branch_id is not None:
                cleanup_db = get_db()
                placeholder = get_placeholder()
                try:
                    cleanup_db.execute(
                        f"DELETE FROM branches WHERE id = {placeholder}",
                        (created_branch_id,),
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