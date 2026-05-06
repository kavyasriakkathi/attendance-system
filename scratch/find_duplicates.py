import re
from collections import Counter

with open('app.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

defs = []
for line in lines:
    m = re.match(r'^def\s+(\w+)', line)
    if m:
        defs.append(m.group(1))

counts = Counter(defs)
duplicates = {name: count for name, count in counts.items() if count > 1}
print(duplicates)
