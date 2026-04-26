import re

with open("app.py", "r", encoding="utf-8") as f:
    content = f.read()

routes = re.findall(r'@app\.route\("([^"]+)"', content)
print("Routes found:")
for r in routes:
    print(f"  {r}")
