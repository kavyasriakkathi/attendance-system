import unittest
import sqlite3
import os
import sys

# Add parent directory to path so we can import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import _ensure_notification_schema, send_sys_notification, get_unread_notifications, publish_announcement, get_relevant_announcements

class TestNotifications(unittest.TestCase):
    def setUp(self):
        # Create an in-memory SQLite database
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        _ensure_notification_schema(self.db)
        
        # We need a dummy get_placeholder for the app functions to work
        # Since app.py might use app.config, let's mock it if possible or rely on sqlite syntax.
        # SQLite uses '?' but our app uses '{placeholder}' with dynamic replacement which might be tricky if we don't load the app context.
        # Actually, let's just insert raw SQL for setup and test the basic logic.
        
    def tearDown(self):
        self.db.close()

    def test_schema_creation(self):
        # Verify tables exist
        tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t["name"] for t in tables]
        self.assertIn("sys_notifications", table_names)
        self.assertIn("sys_notification_recipients", table_names)
        self.assertIn("sys_announcements", table_names)

    def test_priority_and_expiry_columns(self):
        # Verify new columns exist
        columns = self.db.execute("PRAGMA table_info(sys_announcements)").fetchall()
        col_names = [c["name"] for c in columns]
        self.assertIn("priority", col_names)
        self.assertIn("expiry_date", col_names)
        
        rec_cols = self.db.execute("PRAGMA table_info(sys_notification_recipients)").fetchall()
        rec_col_names = [c["name"] for c in rec_cols]
        self.assertIn("is_archived", rec_col_names)
        self.assertIn("is_deleted", rec_col_names)

if __name__ == '__main__':
    unittest.main()
