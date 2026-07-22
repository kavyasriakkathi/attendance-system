import os
import sys
import traceback
from pathlib import Path

# Ensure workspace root is in sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

from app import app, get_db, init_db, row_get, get_teacher_context
from werkzeug.security import generate_password_hash

app.config["TESTING"] = True
app.config["PROPAGATE_EXCEPTIONS"] = True

def run_trace():
    print("--- 1. Initializing DB Schema ---")
    db = get_db()
    init_db(db)
    
    # Ensure teacher tables exist
    from app import _ensure_teacher_schema, _ensure_teacher_support_schema
    _ensure_teacher_schema(db)
    _ensure_teacher_support_schema(db)

    # Seed branch and subject if missing
    branch = db.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
    if not branch:
        db.execute("INSERT INTO branches (name, location) VALUES ('CSE', 'Main Building')")
        db.commit()
        branch = db.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
    branch_id = row_get(branch, "id")

    subject = db.execute("SELECT id FROM subjects ORDER BY id LIMIT 1").fetchone()
    if not subject:
        db.execute("INSERT INTO subjects (name, branch_id) VALUES ('Computer Networks', ?)", (branch_id,))
        db.commit()
        subject = db.execute("SELECT id FROM subjects ORDER BY id LIMIT 1").fetchone()
    subject_id = row_get(subject, "id")

    # Seed user teacher
    username = "teacher_trace_user"
    pw_hash = generate_password_hash("password123")
    
    user = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if not user:
        db.execute("INSERT INTO users (username, password, role) VALUES (?, ?, 'teacher')", (username, pw_hash))
        db.commit()
        user = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    user_id = row_get(user, "id")

    # Seed teacher record
    teacher = db.execute("SELECT id FROM teachers WHERE username = ?", (username,)).fetchone()
    if not teacher:
        db.execute(
            "INSERT INTO teachers (name, username, password, subject_id, branch_id, subject_name) VALUES (?, ?, ?, ?, ?, ?)",
            ("Trace Teacher", username, pw_hash, subject_id, branch_id, "Computer Networks")
        )
        db.commit()
        teacher = db.execute("SELECT id FROM teachers WHERE username = ?", (username,)).fetchone()
    teacher_id = row_get(teacher, "id")

    # Seed assignments
    db.execute("INSERT OR IGNORE INTO teacher_branches (teacher_id, branch_id) VALUES (?, ?)", (teacher_id, branch_id))
    db.execute("INSERT OR IGNORE INTO teacher_subjects (teacher_id, subject_id) VALUES (?, ?)", (teacher_id, subject_id))
    db.execute(
        "INSERT OR IGNORE INTO teacher_subject_assignments (teacher_id, subject_id, branch_id, section, semester) VALUES (?, ?, ?, 'A', '1')",
        (teacher_id, subject_id, branch_id)
    )
    db.commit()
    db.close()

    print("--- 2. Testing Teacher Login POST ---")
    client = app.test_client()
    res = client.post("/teacher_login", data={"username": username, "password": "password123"}, follow_redirects=False)
    print(f"POST /teacher_login response: {res.status_code}")
    print(f"Redirect location: {res.headers.get('Location')}")
    
    with client.session_transaction() as sess:
        print(f"Session data created: user_id={sess.get('user_id')}, teacher_id={sess.get('teacher_id')}, role={sess.get('role')}")

    print("--- 3. Following redirect to Teacher Dashboard ---")
    res_dash = client.get(res.headers.get("Location", "/teacher/dashboard"))
    print(f"GET dashboard status: {res_dash.status_code}")

    if res_dash.status_code != 200:
        print("--- DASHBOARD ERROR CONTENT ---")
        print(res_dash.data.decode("utf-8", errors="ignore")[:2000])

if __name__ == "__main__":
    try:
        run_trace()
    except Exception as e:
        print(f"EXCEPTION TRACEBACK: {repr(e)}")
        traceback.print_exc()
