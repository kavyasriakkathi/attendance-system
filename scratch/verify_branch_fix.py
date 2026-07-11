import sqlite3
conn = sqlite3.connect('attendance.db')
conn.row_factory = sqlite3.Row

print("=== All branches ===")
for row in conn.execute("SELECT id, name FROM branches ORDER BY name").fetchall():
    print(f"  id={row['id']}  name={row['name']!r}")

print()
print("=== Students in ECE-B ===")
rows = conn.execute("""
    SELECT s.name, s.enrollment, b.name AS branch
    FROM students s
    JOIN branches b ON s.branch_id = b.id
    WHERE UPPER(b.name) = 'ECE-B'
    LIMIT 10
""").fetchall()
print(f"  {len(rows)} students found")
for r in rows:
    print(f"  {r['enrollment']}  {r['name']}  -> {r['branch']}")

print()
print("=== Any remaining COPILOT_BRANCH_* ===")
bad = conn.execute("SELECT id, name FROM branches WHERE name LIKE 'COPILOT_BRANCH_%'").fetchall()
if bad:
    for r in bad:
        print(f"  id={r['id']}  name={r['name']!r}")
else:
    print("  None found. All clear.")
conn.close()
