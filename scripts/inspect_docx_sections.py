import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import timetable

path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads', 'B.TECH I-2 TIMETABLE.docx')
print('path', path)
print('exists', os.path.exists(path))

slots = list(timetable.iter_docx_section_slots(path, debug_jsonl_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads', 'docx_inspect_debug.jsonl')))
print('slot_count', len(slots))
sections = {}
for slot in slots:
    sections[slot.get('section')] = sections.get(slot.get('section'), 0) + 1
print('sections', sections)
for slot in slots[:10]:
    print(slot)
