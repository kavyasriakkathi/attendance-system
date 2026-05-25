import os
import json
import traceback
from app import get_db
from timetable import ensure_timetable_tables, parse_pdf_to_slots, import_slots_streaming

BASE = os.path.dirname(__file__)
UPLOADS = os.path.join(BASE, 'uploads')
PDF = os.path.join(UPLOADS, 'CSE-A.pdf')

if not os.path.exists(PDF):
    print('PDF not found:', PDF)
    raise SystemExit(1)

print('Using PDF:', PDF)

try:
    db = get_db()
    ensure_timetable_tables(db)
    # Clear existing timetable
    try:
        print('Deleting existing timetable rows...')
        db.execute('DELETE FROM timetable_slots')
        db.execute('DELETE FROM timetable_entries')
        try:
            db.commit()
        except Exception:
            pass
    except Exception as e:
        print('Failed to delete existing timetable:', e)

    stats = {}
    slots_iter = parse_pdf_to_slots(PDF, stats=stats)
    print('Parsed PDF, starting import...')
    result = import_slots_streaming(db, slots_iter)
    print('Import result:')
    print(json.dumps(result, indent=2, default=str))
    print('Parser stats:')
    print(json.dumps(stats, indent=2, default=str))
    preview_path = os.path.join(BASE, 'uploads', 'last_import_debug.jsonl')
    print('Preview JSONL path:', preview_path)
    if os.path.exists(preview_path):
        print('Preview file size:', os.path.getsize(preview_path))
except Exception:
    traceback.print_exc()
    raise
