import os
import requests
import re
import psycopg2
import psycopg2.extras

PROD_DB_URL = os.environ.get("DATABASE_URL", "")
RENDER_URL = "https://attendance-system-gi39.onrender.com"

def verify_live_add():
    print("=== 1. BEFORE COUNT ON LIVE NEON DB ===")
    conn = psycopg2.connect(PROD_DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) FROM teachers;")
    cnt_before = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) FROM users WHERE role='teacher';")
    cnt_u_before = cur.fetchone()["count"]
    print(f"Teachers count before: {cnt_before}, Users(teacher) count before: {cnt_u_before}")

    session = requests.Session()
    # Login as admin
    session.post(f"{RENDER_URL}/login", data={"username": "admin", "password": "admin123"})

    test_username = "test_live_teacher_xyz"
    print(f"\n=== 2. SUBMITTING ADD TEACHER FORM ON LIVE RENDER FOR '{test_username}' ===")
    res = session.post(
        f"{RENDER_URL}/admin/teachers",
        data={
            "action": "add",
            "name": "Test Live Teacher",
            "username": test_username,
            "password": "teacher123",
            "email": "live_test@example.com",
            "phone": "9998887777",
            "status": "active",
            "assign_subject_id[]": ["15"],
            "assign_branch_id[]": ["1"],
            "assign_section[]": ["A"],
            "assign_semester[]": ["1"],
        },
        allow_redirects=True
    )
    print("Add Form HTTP Status:", res.status_code)
    flashes = re.findall(r'class="flash[^"]*"[^>]*>(.*?)</div>', res.text, re.DOTALL)
    print("Flash Messages:", [f.strip() for f in flashes])
    in_list = test_username in res.text
    print(f"Appears in /admin/teachers HTML list? -> {in_list}")

    print("\n=== 3. AFTER COUNT ON LIVE NEON DB ===")
    cur.execute("SELECT COUNT(*) FROM teachers;")
    cnt_after = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) FROM users WHERE role='teacher';")
    cnt_u_after = cur.fetchone()["count"]
    print(f"Teachers count after: {cnt_after}, Users(teacher) count after: {cnt_u_after}")

    print(f"\n=== 4. TESTING LOGIN FOR '{test_username}' ON LIVE RENDER ===")
    tsess = requests.Session()
    login_res = tsess.post(f"{RENDER_URL}/teacher_login", data={"username": test_username, "password": "teacher123"}, allow_redirects=True)
    print("Teacher Login HTTP Status:", login_res.status_code, "Final URL:", login_res.url)

    # Cleanup test user
    print("\n=== 5. CLEANING UP TEST USER FROM LIVE NEON DB ===")
    cur.execute("DELETE FROM teacher_subject_assignments WHERE teacher_id IN (SELECT id FROM teachers WHERE username = %s);", (test_username,))
    cur.execute("DELETE FROM teachers WHERE username = %s;", (test_username,))
    cur.execute("DELETE FROM users WHERE username = %s;", (test_username,))
    conn.commit()
    print("Cleanup completed successfully.")

    cur.close()
    conn.close()

if __name__ == "__main__":
    verify_live_add()
