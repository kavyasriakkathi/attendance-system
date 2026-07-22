import os
from app import app, get_db

with app.app_context():
    db = get_db()
    rows = db.execute("SELECT b.id, b.name, count(s.id) as c FROM branches b LEFT JOIN students s ON s.branch_id = b.id GROUP BY b.id, b.name ORDER BY c DESC").fetchall()
    for r in rows:
        bid = r[0] if isinstance(r, tuple) else r['id']
        bname = r[1] if isinstance(r, tuple) else r['name']
        c = r[2] if isinstance(r, tuple) else r['c']
        print(f"Branch: {bname} (ID: {bid}), Students: {c}")
