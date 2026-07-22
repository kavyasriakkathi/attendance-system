import requests
import re

base_url = "https://attendance-system-gi39.onrender.com"
session = requests.Session()

print("=== STEP 1: LOGIN AS TEACHER1 ON RENDER ===")
login_res = session.post(f"{base_url}/teacher_login", data={"username": "teacher1", "password": "1234"}, allow_redirects=True)
print("Login redirect status:", login_res.status_code, "URL:", login_res.url)

routes = [
    "/teacher/dashboard",
    "/teacher_dashboard",
    "/teacher/select-branch",
    "/teacher/select-subject",
    "/teacher/mark-attendance",
    "/teacher/attendance",
    "/teacher/history",
    "/teacher/profile",
]

print("\n=== STEP 2: TESTING ALL TEACHER ROUTES ON LIVE RENDER ===")
for r in routes:
    res = session.get(f"{base_url}{r}", allow_redirects=True)
    if res.status_code == 500 or "Internal Server Error" in res.text:
        print(f"  [500 ERROR DETECTED!] {r} -> HTTP {res.status_code}")
        print("  Response Content Snippet:")
        print(res.text[:1000])
    else:
        print(f"  [OK] {r} -> HTTP {res.status_code} (URL: {res.url})")
