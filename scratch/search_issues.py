import re

with open("app.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "fetchone()[0]" in line:
        print(f"Found on line {i+1}: {line.strip()}")
    if "round(" in line and "/" in line and "if" not in line:
         # Potential division by zero without check
         print(f"Potential div by zero on line {i+1}: {line.strip()}")
