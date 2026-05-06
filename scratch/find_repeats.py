import collections

with open('app.py', 'r', encoding='utf-8') as f:
    lines = [line.strip() for line in f if line.strip()]

line_counts = collections.Counter(lines)
for line, count in line_counts.items():
    if count > 5 and len(line) > 50:
        print(f"Count: {count}, Line: {line}")
