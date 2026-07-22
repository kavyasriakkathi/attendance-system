import os
from app import app, get_db

with app.app_context():
    db = get_db()
    rows = db.execute("SELECT id, name, enrollment, branch_id FROM students WHERE enrollment LIKE '25TQ1A56%'").fetchall()
    print(f"Found {len(rows)} students with enrollment starting with 25TQ1A56")
    for r in rows:
        bid = r['branch_id'] if not isinstance(r, tuple) else r[3]
        print(f"Enrollment: {r['enrollment']}, Branch ID: {bid}")
