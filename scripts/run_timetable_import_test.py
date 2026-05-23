import time
import tracemalloc
import json
import os
import sys

# Ensure project root is on path
PR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PR not in sys.path:
    sys.path.insert(0, PR)

from app import get_db
import timetable

# Ensure timetable module has json available for its preview writes
import json as _json
setattr(timetable, 'json', _json)

UPLOADS_DIR = os.path.join(os.path.dirname(PR), 'uploads')
if not os.path.exists(UPLOADS_DIR):
    UPLOADS_DIR = os.path.join(PR, 'uploads')

DOCX = os.path.join(UPLOADS_DIR, 'timetable_upload.docx')
PDF = os.path.join(UPLOADS_DIR, 'CSE-A.pdf')
if not os.path.exists(PDF):
    try:
        for name in os.listdir(UPLOADS_DIR):
            if name.lower().endswith('.pdf'):
                PDF = os.path.join(UPLOADS_DIR, name)
                break
    except Exception:
        PDF = ''

out = {
    'parsed_slots': 0,
    'streamed_slots': 0,
    'inserted': 0,
    'inserted_normalized': 0,
    'skipped': 0,
    'skipped_normalized': 0,
    'batch_commits': [],
    'elapsed_seconds': None,
    'memory_peak_bytes': None,
    'problematic_merged_cells': [],
    'faculty_issues_sample': [],
    'preview_path': None,
}

start = time.time()
tracemalloc.start()

try:
    db = get_db()
    timetable.ensure_timetable_tables(db)

    slots = []
    faculty_issues = []
    try:
        if PDF and os.path.exists(PDF):
            pdf_stats = {}
            def _slot_source():
                for slot in timetable.parse_pdf_to_slots(PDF, stats=pdf_stats):
                    slots.append(slot)
                    if not (slot.get('faculty_name') and str(slot.get('faculty_name')).strip()):
                        faculty_issues.append(slot)
                    yield slot

            import_info = timetable.import_slots_streaming(db, _slot_source())
            out['streamed_slots'] = int(import_info.get('raw_insert', {}).get('counters', {}).get('processed', 0))
            out['parsed_slots'] = len(slots)
            out['source_file'] = PDF
            out['source_type'] = 'pdf'
            out['pdf_stats'] = pdf_stats
        elif os.path.exists(DOCX):
            def _slot_source():
                for slot in timetable.iter_docx_section_slots(DOCX, debug_jsonl_path=os.path.join(os.path.dirname(__file__), '..', 'uploads', 'last_import_debug.jsonl')):
                    slots.append(slot)
                    if not (slot.get('faculty_name') and str(slot.get('faculty_name')).strip()):
                        faculty_issues.append(slot)
                    yield slot

            import_info = timetable.import_slots_streaming(db, _slot_source())
            out['streamed_slots'] = int(import_info.get('raw_insert', {}).get('counters', {}).get('processed', 0))
            out['parsed_slots'] = len(slots)
            out['source_file'] = DOCX
            out['source_type'] = 'docx'
        else:
            raise FileNotFoundError(DOCX)
    except Exception as e:
        print('STREAM_PARSE_FAILED', e)
        raise

    out['inserted'] = int(import_info.get('raw_insert', {}).get('counters', {}).get('inserted', 0))
    out['skipped'] = int(import_info.get('raw_insert', {}).get('counters', {}).get('skipped_total', 0))
    out['inserted_normalized'] = int(import_info.get('normalized_insert', {}).get('counters', {}).get('inserted', 0))
    out['skipped_normalized'] = int(import_info.get('normalized_insert', {}).get('counters', {}).get('skipped_total', 0))
    out['preview_path'] = import_info.get('preview_path')

    try:
        debug_jsonl = os.path.join(os.path.dirname(__file__), '..', 'uploads', 'last_import_debug.jsonl')
        if os.path.exists(debug_jsonl):
            with open(debug_jsonl, 'r', encoding='utf-8') as handle:
                out['debug_jsonl_lines'] = sum(1 for _ in handle)
    except Exception:
        out['debug_jsonl_lines'] = None

    # faculty issues: samples where faculty_name is empty
    out['faculty_issues_sample'] = faculty_issues[:20]

    # attempt to capture DB duplicate diagnostics
    try:
        diag = timetable._table_diagnostics(db)
        out['duplicates'] = diag
    except Exception:
        out['duplicates'] = None

except Exception as e:
    out['error'] = repr(e)

current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
end = time.time()
out['elapsed_seconds'] = end - start
out['memory_peak_bytes'] = peak

# Write a copy of out to uploads/last_import_summary.json for convenience
try:
    summary_path = os.path.join(os.path.dirname(__file__), '..', 'uploads', 'last_import_summary.json')
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2, default=str)
except Exception:
    pass

print(json.dumps(out, indent=2, default=str))
