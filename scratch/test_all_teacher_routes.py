import os
import sys
import traceback
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

import app as app_module
from app import app, get_db, init_db, row_get, get_teacher_context
from werkzeug.security import generate_password_hash

app.config["TESTING"] = True
app.config["PROPAGATE_EXCEPTIONS"] = True

def run_route_tests():
    db = get_db()
    init_db(db)
    from app import _ensure_teacher_schema, _ensure_teacher_support_schema
    _ensure_teacher_schema(db)
    _ensure_teacher_support_schema(db)

    # Seed test teacher
    username = "teacher_full_test"
    pw_hash = generate_password_hash("password123")
    
    # Check or insert user
    user = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if not user:
        db.execute("INSERT INTO users (username, password, role) VALUES (?, ?, 'teacher')", (username, pw_hash))
        db.commit()
        user = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    user_id = row_get(user, "id")

    # Check or insert branch
    branch = db.execute("SELECT id FROM branches LIMIT 1").fetchone()
    if not branch:
        db.execute("INSERT INTO branches (name, location) VALUES ('CSE', 'Building A')")
        db.commit()
        branch = db.execute("SELECT id FROM branches LIMIT 1").fetchone()
    branch_id = row_get(branch, "id")

    # Check or insert subject
    subject = db.execute("SELECT id FROM subjects LIMIT 1").fetchone()
    if not subject:
        db.execute("INSERT INTO subjects (name, branch_id) VALUES ('Database Systems', ?)", (branch_id,))
        db.commit()
        subject = db.execute("SELECT id FROM subjects LIMIT 1").fetchone()
    subject_id = row_get(subject, "id")

    # Check or insert teacher
    teacher = db.execute("SELECT id FROM teachers WHERE username = ?", (username,)).fetchone()
    if not teacher:
        db.execute(
            "INSERT INTO teachers (id, name, username, password, subject_id, branch_id, subject_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, "Full Test Teacher", username, pw_hash, subject_id, branch_id, "Database Systems")
        )
        db.commit()

    # Seed assignments
    db.execute("INSERT OR IGNORE INTO teacher_branches (teacher_id, branch_id) VALUES (?, ?)", (user_id, branch_id))
    db.execute("INSERT OR IGNORE INTO teacher_subjects (teacher_id, subject_id) VALUES (?, ?)", (user_id, subject_id))
    db.execute(
        "INSERT OR IGNORE INTO teacher_subject_assignments (teacher_id, subject_id, branch_id, section, semester) VALUES (?, ?, ?, 'A', '1')",
        (user_id, subject_id, branch_id)
    )
    db.commit()
    db.close()

    client = app.test_client()

    routes_to_test = [
        ("GET", "/teacher_login"),
        ("POST", "/teacher_login", {"username": username, "password": "password123"}),
        ("GET", "/teacher/dashboard"),
        ("GET", "/teacher-dashboard"),
        ("GET", "/teacher/select-branch"),
        ("GET", "/teacher/select-subject"),
        ("GET", "/teacher-mark-attendance"),
        ("GET", "/teacher/records"),
    ]

    print("--- STARTING ROUTE EXECUTION CHECKS ---")
    
    # Login first
    login_res = client.post("/teacher_login", data={"username": username, "password": "password123"})
    print(f"POST /teacher_login -> {login_res.status_code}")

    for method, path, *data_arg in routes_to_test[2:]:
        data = data_arg[0] if data_arg else None
        try:
            if method == "GET":
                res = client.get(path)
            else:
                res = client.post(path, data=data)
            print(f"{method} {path} -> HTTP {res.status_code}")
            if res.status_code == 500:
                print(f"500 ERROR ON {path}!")
                print(res.data.decode("utf-8", errors="ignore")[:2000])
        except Exception as e:
            print(f"EXCEPTIONAL FAIL ON {method} {path}: {repr(e)}")
            traceback.print_exc()

if __name__ == "__main__":
    run_route_tests()
