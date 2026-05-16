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

DOCX = os.path.join(os.path.dirname(PR), 'uploads', 'timetable_upload.docx')
if not os.path.exists(DOCX):
    DOCX = os.path.join(PR, 'uploads', 'timetable_upload.docx')

out = {
    'parsed_slots': 0,
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

    # parse using grid parser explicitly
    slots = []
    try:
        slots = timetable.parse_docx_grid(DOCX)
    except Exception as e:
        print('PARSE_GRID_FAILED', e)
        raise

    out['parsed_slots'] = len(slots)

    # quick merged-cell detection: scan docx for gridSpan/vMerge markers
    try:
        if timetable.docx:
            doc = timetable.docx.Document(DOCX)
            for ti, table in enumerate(doc.tables):
                for ri, row in enumerate(table.rows):
                    for ci, cell in enumerate(row.cells):
                        tc_xml = cell._tc.xml if hasattr(cell, '_tc') else ''
                        if 'gridSpan' in tc_xml or 'vMerge' in tc_xml:
                            out['problematic_merged_cells'].append({'table': ti, 'row': ri, 'col': ci, 'text': cell.text[:200]})
    except Exception:
        pass

    # run import_slots (raw)
    ins = timetable.import_slots(db, slots)
    nc = timetable.import_slots_normalized(db, slots)

    counters = ins.get('counters', {})
    nc_counters = nc.get('counters', {})
    out['inserted'] = int(counters.get('inserted', 0))
    out['skipped'] = int(counters.get('skipped_total', 0))
    out['inserted_normalized'] = int(nc_counters.get('inserted', 0))
    out['skipped_normalized'] = int(nc_counters.get('skipped_total', 0))
    out['preview_path'] = ins.get('preview_path') or nc.get('preview_path')

    # faculty issues: samples where faculty_name is empty
    fac_issues = [s for s in slots if not (s.get('faculty_name') and s.get('faculty_name').strip())]
    out['faculty_issues_sample'] = fac_issues[:20]

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
