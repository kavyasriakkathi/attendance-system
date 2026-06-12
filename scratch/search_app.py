with open("app.py", "r", encoding="utf-8", errors="ignore") as f:
    for i, line in enumerate(f, 1):
        if "def login" in line or "@app.route" in line:
            print(f"{i}: {line.strip()}")
