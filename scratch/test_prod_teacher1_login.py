import requests
import re

url = "https://attendance-system-gi39.onrender.com/teacher_login"
session = requests.Session()

print("--- SUBMITTING TEACHER LOGIN ON RENDER FOR 'teacher1' WITH PASSWORD '1234' ---")
res = session.post(url, data={"username": "teacher1", "password": "1234"}, allow_redirects=True)
print("Final Status Code:", res.status_code)
print("Final URL:", res.url)

if res.status_code == 500 or "Internal Server Error" in res.text:
    print("\n*** REPRODUCED PRODUCTION HTTP 500 ERROR ***")
    print(res.text[:1500])
else:
    print("\n--- RESPONSE SUMMARY ---")
    flashes = re.findall(r'class="flash[^"]*"[^>]*>(.*?)</div>', res.text, re.DOTALL)
    print("Flash messages:", [f.strip() for f in flashes])
    print("Page Title / Heading:", re.findall(r'<h[1-3][^>]*>(.*?)</h[1-3]>', res.text, re.DOTALL))
