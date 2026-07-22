import os
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

import app as app_module
from app import app, get_db, row_get

def test_full_add_teacher_flow():
    client = app.test_client()
    
    # 1. Admin login session
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = "admin"
        sess["role"] = "admin"
        
    with app.app_context():
        db = get_db()
        cnt_teachers_before = db.execute("SELECT COUNT(*) AS count FROM teachers").fetchone()
        cnt_users_before = db.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        print(f"BEFORE ADD: teachers count = {row_get(cnt_teachers_before, 'count')}, users count = {row_get(cnt_users_before, 'count')}")

    print("\n--- Submitting Add Teacher form for 'test_siddheshwar_form' ---")
    res = client.post(
        "/admin/teachers",
        data={
            "action": "add",
            "name": "Siddheshwar Verification",
            "username": "test_siddheshwar_form",
            "password": "teacher123",
            "email": "siddheshwar@example.com",
            "phone": "9876543210",
            "status": "active",
            "assign_subject_id[]": ["15"],
            "assign_branch_id[]": ["1"],
            "assign_section[]": ["A"],
            "assign_semester[]": ["1"],
        },
        follow_redirects=True
    )
    print("Form Submission Status Code:", res.status_code)
    
    with app.app_context():
        db = get_db()
        cnt_teachers_after = db.execute("SELECT COUNT(*) AS count FROM teachers").fetchone()
        cnt_users_after = db.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        t_row = db.execute("SELECT id, name, username FROM teachers WHERE username = 'test_siddheshwar_form'").fetchone()
        u_row = db.execute("SELECT id, username, role FROM users WHERE username = 'test_siddheshwar_form'").fetchone()
        print(f"AFTER ADD: teachers count = {row_get(cnt_teachers_after, 'count')}, users count = {row_get(cnt_users_after, 'count')}")
        print("Inserted teacher row:", dict(t_row) if t_row else None)
        print("Inserted user row:", dict(u_row) if u_row else None)

    print("\n--- Verifying Login for 'test_siddheshwar_form' ---")
    tclient = app.test_client()
    login_res = tclient.post("/teacher_login", data={"username": "test_siddheshwar_form", "password": "teacher123"}, follow_redirects=False)
    print("Teacher Login HTTP Status Code:", login_res.status_code, "Redirect Location:", login_res.headers.get("Location"))
    
    dash_res = tclient.get("/teacher/dashboard")
    print("Teacher Dashboard HTTP Status Code:", dash_res.status_code)
    
    # Cleanup test row
    with app.app_context():
        db = get_db()
        db.execute("DELETE FROM teacher_subject_assignments WHERE teacher_id IN (SELECT id FROM teachers WHERE username = 'test_siddheshwar_form')")
        db.execute("DELETE FROM teachers WHERE username = 'test_siddheshwar_form'")
        db.execute("DELETE FROM users WHERE username = 'test_siddheshwar_form'")
        db.commit()
        print("Cleanup of test user completed.")

if __name__ == "__main__":
    test_full_add_teacher_flow()
