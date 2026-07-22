import requests
import re

base_url = "https://attendance-system-gi39.onrender.com"
session = requests.Session()

print("=== 1. ADMIN LOGIN ON RENDER ===")
login_res = session.post(f"{base_url}/login", data={"username": "admin", "password": "admin123"}, allow_redirects=True)
print("Admin Login Status:", login_res.status_code, "URL:", login_res.url)

print("\n=== 2. SUBMITTING ADD TEACHER FORM ON LIVE RENDER ===")
data = {
    "action": "add",
    "name": "Siddheshwar",
    "username": "siddheshwar",
    "password": "teacher123",
    "email": "siddheshwar@example.com",
    "phone": "9876543210",
    "status": "active",
    "assign_subject_id[]": ["1"],
    "assign_branch_id[]": ["1"],
    "assign_section[]": ["A"],
    "assign_semester[]": ["1"],
}

add_res = session.post(f"{base_url}/admin/teachers", data=data, allow_redirects=True)
print("Add Teacher Form Response Status Code:", add_res.status_code)
print("URL:", add_res.url)

if add_res.status_code == 500 or "Internal Server Error" in add_res.text:
    print("\n*** EXCEPTION / 500 ERROR FOUND ON RENDER! ***")
    print(add_res.text[:1500])
else:
    flashes = re.findall(r'class="flash[^"]*"[^>]*>(.*?)</div>', add_res.text, re.DOTALL)
    print("Flash Messages:", [f.strip() for f in flashes])
    
    # Verify Teacher List on Admin page
    teachers = re.findall(r'<td>(.*?)</td>', add_res.text)
    print("Table cells snippet:", teachers[:10])
