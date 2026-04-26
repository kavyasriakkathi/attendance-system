import re

with open("app.py", "r", encoding="utf-8") as f:
    content = f.read()

# Search for any fetchone followed by [0]
matches = re.finditer(r'fetchone\(\)\s*\[0\]', content)
print("Potential fetchone index access found:")
for m in matches:
    # Get line number
    line_no = content.count('\n', 0, m.start()) + 1
    print(f"  Line {line_no}: {m.group()}")

# Search for division
divs = re.finditer(r'/\s*COUNT\(\*\)', content)
print("\nPotential division by COUNT(*) found:")
for m in divs:
    line_no = content.count('\n', 0, m.start()) + 1
    print(f"  Line {line_no}: {m.group()}")
