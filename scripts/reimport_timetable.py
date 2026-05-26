import os, sys, traceback
# Ensure project root is on sys.path so top-level modules (app, timetable) import correctly
sys.path.insert(0, os.getcwd())
from app import get_db
import timetable

pdf = os.path.join(os.getcwd(), 'uploads', 'CSE-A.pdf')
print('PDF path:', pdf)

try:
    db = get_db()
    print('DB acquired')

    try:
        db.execute("DELETE FROM timetable_entries")
        db.commit()
        print('Cleared timetable_entries')
    except Exception as e:
        print('Failed to clear timetable_entries:', e)

    stats = {}
    slots = list(timetable.parse_pdf_to_slots(pdf, stats=stats))
    print('Parsed slots:', len(slots), 'stats:', stats)

    res = timetable.import_slots_streaming(db, iter(slots))
    print('Import result:', res)

    c = db.execute('SELECT COUNT(1) FROM timetable_entries').fetchone()[0]
    print('timetable_entries count =', int(c or 0))

    rows = db.execute("SELECT te.day, te.start_time, te.end_time, te.section, te.semester, COALESCE(s.name,'') AS subject_name, COALESCE(t.name,'') AS faculty_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id LIMIT 10").fetchall()
    for r in rows:
        print(dict(r))

    db.close()
except Exception:
    traceback.print_exc()
