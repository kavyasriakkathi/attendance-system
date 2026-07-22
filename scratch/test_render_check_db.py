import requests

base_url = "https://attendance-system-gi39.onrender.com"
session = requests.Session()

print("--- Attempting Admin Login on Render ---")
passwords = ["admin123", "admin", "1234", "password", "admin@123"]
logged_in = False
for pwd in passwords:
    res = session.post(f"{base_url}/login", data={"username": "admin", "password": pwd}, allow_redirects=True)
    if "/dashboard" in res.url or "Admin" in res.text or "Logout" in res.text:
        print(f"SUCCESS: Admin logged in with password '{pwd}'! Final URL: {res.url}")
        logged_in = True
        break
    else:
        print(f"Admin login failed with password '{pwd}'. Status: {res.status_code}")

if logged_in:
    print("\n--- Calling /admin/check-db ---")
    chk_res = session.get(f"{base_url}/admin/check-db")
    print("Check-DB Status Code:", chk_res.status_code)
    print("Check-DB Output:")
    print(chk_res.text)

    print("\n--- Calling /admin/teachers ---")
    t_res = session.get(f"{base_url}/admin/teachers")
    print("Admin Teachers Page Status Code:", t_res.status_code)
    if t_res.status_code == 500:
        print("ADMIN TEACHERS PAGE 500 INTERNAL SERVER ERROR!")
        print(t_res.text[:1500])
