import os
import sys
import sqlite3
import traceback
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

import app as app_module
from app import app, init_db, row_get, get_teacher_context
from werkzeug.security import generate_password_hash

app.config["TESTING"] = True
app.config["PROPAGATE_EXCEPTIONS"] = True
app.config["DATABASE"] = "postgresql://mock_user:mock_pass@ep-cool-12345.ap-south-1.aws.neon.tech/attendance?sslmode=require"

sqlite_test_db = sqlite3.connect(":memory:")

# Setup information_schema table in sqlite_test_db for PostgreSQL column checks
sqlite_test_db.execute("""
CREATE TABLE IF NOT EXISTS information_schema_columns (
    table_schema TEXT,
    table_name TEXT,
    column_name TEXT,
    ordinal_position INT
);
""")

class MockPsycopg2Cursor:
    def __init__(self, sqlite_conn):
        self._conn = sqlite_conn
        self._cur = sqlite_conn.cursor()
        self.rowcount = -1

    def execute(self, query, params=()):
        sql = query
        # Handle Postgres SERIAL primary key translation for SQLite mock
        sql = sql.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        sql = sql.replace("information_schema.columns", "information_schema_columns")
        sql = sql.replace("%s", "?")
        sql = sql.replace("ILIKE", "LIKE")
        if "RETURNING id" in sql:
            sql = sql.replace("RETURNING id", "")

        try:
            self._cur.execute(sql, params)
            self.rowcount = self._cur.rowcount
            
            # If DDL CREATE TABLE, register columns in information_schema_columns
            if "CREATE TABLE IF NOT EXISTS" in query:
                table_name = query.split("CREATE TABLE IF NOT EXISTS")[1].split("(")[0].strip()
                # Exclude columns if already inserted
                self._conn.execute("DELETE FROM information_schema_columns WHERE table_name = ?", (table_name,))
                # Very simple column parser for mock
                body = query.split("(", 1)[1].rsplit(")", 1)[0]
                pos = 1
                for line in body.split("\n"):
                    line = line.strip().rstrip(",")
                    if line and not line.startswith("UNIQUE") and not line.startswith("PRIMARY") and not line.startswith("FOREIGN") and not line.startswith("CONSTRAINT"):
                        col_name = line.split()[0]
                        self._conn.execute(
                            "INSERT INTO information_schema_columns VALUES ('public', ?, ?, ?)",
                            (table_name, col_name, pos)
                        )
                        pos += 1
        except Exception as err:
            # print("FAILED SQL:", sql, params)
            raise err
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description]
        return dict(zip(cols, row))

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._cur.description]
        return [dict(zip(cols, r)) for r in rows]

class MockPsycopg2Conn:
    def __init__(self, sqlite_conn):
        self._conn = sqlite_conn
    def cursor(self, cursor_factory=None):
        return MockPsycopg2Cursor(self._conn)
    def set_session(self, autocommit=False):
        pass
    def commit(self):
        self._conn.commit()
    def rollback(self):
        self._conn.rollback()
    def close(self):
        self._conn.close()

def mock_psycopg2_connect(*args, **kwargs):
    return MockPsycopg2Conn(sqlite_test_db)

import psycopg2
psycopg2.connect = mock_psycopg2_connect

def test_full_teacher_flow():
    print("=== STEP 1: INITIALIZE POSTGRES DB SCHEMA ===")
    app_module._DB_INIT_DONE = False
    from app import get_db, _ensure_teacher_schema, _ensure_teacher_support_schema
    db = get_db()
    init_db(db)
    _ensure_teacher_schema(db)
    _ensure_teacher_support_schema(db)

    print("=== STEP 2: SEED TEACHER & ASSIGNMENT DATA ===")
    
    # Branch & Subject
    db.execute("INSERT INTO branches (name, location) VALUES ('CSE', 'Main Building')")
    db.commit()
    branch = db.execute("SELECT id FROM branches WHERE name = %s", ("CSE",)).fetchone()
    branch_id = row_get(branch, "id")
    print(f"Created branch_id: {branch_id}")

    db.execute("INSERT INTO subjects (name, branch_id) VALUES ('Operating Systems', %s)", (branch_id,))
    db.commit()
    subject = db.execute("SELECT id FROM subjects WHERE name = %s", ("Operating Systems",)).fetchone()
    subject_id = row_get(subject, "id")
    print(f"Created subject_id: {subject_id}")

    # User & Teacher
    username = "teacher_pg_test"
    pw_hash = generate_password_hash("pass123")

    db.execute("INSERT INTO users (username, password, role) VALUES (%s, %s, 'teacher')", (username, pw_hash))
    db.commit()
    user = db.execute("SELECT id FROM users WHERE username = %s", (username,)).fetchone()
    user_id = row_get(user, "id")
    print(f"Created user_id: {user_id}")

    db.execute(
        "INSERT INTO teachers (id, name, username, password, subject_id, branch_id, subject_name) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (user_id, "PG Teacher", username, pw_hash, subject_id, branch_id, "Operating Systems")
    )
    db.commit()

    # Teacher Assignments
    db.execute("INSERT INTO teacher_branches (teacher_id, branch_id) VALUES (%s, %s)", (user_id, branch_id))
    db.execute("INSERT INTO teacher_subjects (teacher_id, subject_id) VALUES (%s, %s)", (user_id, subject_id))
    db.execute(
        "INSERT INTO teacher_subject_assignments (teacher_id, subject_id, branch_id, section, semester) VALUES (%s, %s, %s, 'A', '1')",
        (user_id, subject_id, branch_id)
    )
    db.commit()

    print("=== STEP 3: TEST POST /teacher_login ===")
    client = app.test_client()
    login_res = client.post("/teacher_login", data={"username": username, "password": "pass123"}, follow_redirects=False)
    print(f"Login Response: {login_res.status_code}, Location: {login_res.headers.get('Location')}")

    with client.session_transaction() as sess:
        print(f"Session state: user_id={sess.get('user_id')}, teacher_id={sess.get('teacher_id')}, role={sess.get('role')}")

    print("=== STEP 4: TEST GET /teacher/dashboard ===")
    dash_res = client.get("/teacher/dashboard")
    print(f"Dashboard Response status code: {dash_res.status_code}")

    if dash_res.status_code != 200:
        print("--- DASHBOARD FAILURE DATA ---")
        print(dash_res.data.decode("utf-8", errors="ignore")[:3000])
    else:
        print("SUCCESS! Dashboard rendered HTTP 200")

    print("=== STEP 5: TEST get_teacher_context() DIRECTLY ===")
    with app.test_request_context("/teacher/dashboard"):
        from flask import session
        session["user_id"] = user_id
        session["teacher_id"] = user_id
        session["role"] = "teacher"
        ctx = get_teacher_context(db)
        print("get_teacher_context result keys:", list(ctx.keys()) if ctx else None)
        if ctx:
            print("Teacher name:", ctx.get("name"))
            print("Current branch:", ctx.get("current_branch_name"))
            print("Current subject:", ctx.get("subject_name"))

if __name__ == "__main__":
    try:
        test_full_teacher_flow()
    except Exception as e:
        print("\n!!! EXCEPTION CAUGHT IN POSTGRES FLOW TEST !!!")
        print("Type:", type(e).__name__)
        print("Message:", str(e))
        traceback.print_exc()
