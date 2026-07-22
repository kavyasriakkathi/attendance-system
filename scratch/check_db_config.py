import os
from pathlib import Path
from app import app, get_db

with app.app_context():
    db_url = app.config.get('DATABASE', '')
    print(f"Database URL in app config: {db_url}")
    db = get_db()
    csw_row = db.execute("SELECT id, name FROM branches WHERE name = 'CSW'").fetchone()
    if csw_row:
        csw_id = csw_row[0] if isinstance(csw_row, tuple) else csw_row['id']
        print(f"CSW Branch ID: {csw_id}")
        count = db.execute("SELECT count(*) as c FROM students WHERE branch_id = ?", (csw_id,)).fetchone()
        c = count[0] if isinstance(count, tuple) else count['c']
        print(f"Students in CSW: {c}")
    else:
        print("CSW branch not found")
