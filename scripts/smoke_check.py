import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import get_db, _resolve_timetable_slots, normalize_text

cases = [
    ("CSE", "BEE"),
    ("CSM", "ODEVC"),
    ("CSE", "Data Structures"),
]

db = get_db()
for branch_q, subject_q in cases:
    print("\n=== Smoke: branch=", branch_q, " subject=", subject_q)
    # try to find ids
    placeholder = '?'  # sqlite placeholder will be replaced by API but we use direct queries
    try:
        b = db.execute("SELECT id, name FROM branches WHERE LOWER(name) LIKE ? LIMIT 1", (f"%{branch_q.lower()}%",)).fetchone()
        s = db.execute("SELECT id, name FROM subjects WHERE LOWER(name) LIKE ? LIMIT 1", (f"%{subject_q.lower()}%",)).fetchone()
        print('found branch:', b and (b['id'], b['name']))
        print('found subject:', s and (s['id'], s['name']))
        branch_id = b['id'] if b else ''
        subject_id = s['id'] if s else ''
        ctx = _resolve_timetable_slots(db, branch_id=branch_id, subject_id=subject_id, selected_date=None, section='', period='')
        print('slots_count=', len(ctx.get('slots', [])))
        print('selected_slot=', ctx.get('selected_slot'))
        print('active_slot=', ctx.get('active_slot'))
        print('has_schedule=', ctx.get('has_schedule'))
    except Exception as e:
        print('Error during smoke check:', e)

try:
    db.close()
except Exception:
    pass

print('\nSmoke checks complete.')
