import requests

url = "https://attendance-system-gi39.onrender.com/teacher_login"
session = requests.Session()

# 1. GET
get_res = session.get(url)

# 2. POST for various test users
usernames = ["siddheshwar", "teacher1", "tuser", "admin"]
for u in usernames:
    print(f"\n--- Testing login for '{u}' ---")
    res = session.post(url, data={"username": u, "password": "teacher123"}, allow_redirects=True)
    print("Status Code:", res.status_code)
    print("URL after redirects:", res.url)
    if "Internal Server Error" in res.text:
        print("RESULT: 500 Internal Server Error!")
    else:
        # Extract flash or text
        import re
        flashes = re.findall(r'class="flash[^"]*"[^>]*>(.*?)</div>', res.text, re.DOTALL)
        print("Flash messages:", [f.strip() for f in flashes])
        print("Page title/heading:", re.findall(r'<h[1-3][^>]*>(.*?)</h[1-3]>', res.text, re.DOTALL))
