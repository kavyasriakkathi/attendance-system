import requests
import re

base_url = "https://attendance-system-gi39.onrender.com"
session = requests.Session()

print("=== TESTING LOGIN FOR 'siddheshwar' ON LIVE RENDER ===")
res = session.post(f"{base_url}/teacher_login", data={"username": "siddheshwar", "password": "teacher123"}, allow_redirects=True)
print("Final Status Code:", res.status_code)
print("Final URL:", res.url)

if res.status_code == 500 or "Internal Server Error" in res.text:
    print("\n*** EXCEPTION / 500 ERROR ON TEACHER LOGIN! ***")
    print(res.text[:1500])
else:
    flashes = re.findall(r'class="flash[^"]*"[^>]*>(.*?)</div>', res.text, re.DOTALL)
    print("Flash Messages:", [f.strip() for f in flashes])
    print("Page Content Snippet:")
    print(res.text[:800])
