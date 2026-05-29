import os, sys, traceback
# Ensure project root is on sys.path so top-level modules (app, timetable) import correctly
sys.path.insert(0, os.getcwd())
from app import get_db
import timetable

upload_dir = os.path.join(os.getcwd(), 'uploads')
docx_candidates = [
    os.path.join(upload_dir, 'B.TECH I-2 TIMETABLE.docx'),
    os.path.join(upload_dir, 'timetable_upload.docx'),
]
docx = next((path for path in docx_candidates if os.path.exists(path)), docx_candidates[0])
pdf = os.path.join(upload_dir, 'CSE-A.pdf')
print('DOCX path:', docx)
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
    if os.path.exists(docx):
        slots = list(timetable.iter_docx_section_slots(docx))
        print('Parsed DOCX slots:', len(slots), 'stats:', stats)
    else:
        slots = list(timetable.parse_pdf_to_slots(pdf, stats=stats))
        print('Parsed PDF slots:', len(slots), 'stats:', stats)

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
