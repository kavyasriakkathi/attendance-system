import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import get_db

db = get_db()
rows = db.execute("""
    SELECT te.branch_id, te.section, te.day, te.start_time, te.end_time, te.subject_id, te.teacher_id, te.subject_name, te.faculty_name, te.room
    FROM timetable_entries te
    LEFT JOIN branches b ON te.branch_id = b.id
    WHERE te.section LIKE '%CSM%' OR b.name LIKE '%CSM%'
    LIMIT 20
""").fetchall()
print('count', len(rows))
for row in rows[:10]:
    print(dict(row))

nulls = db.execute("""
    SELECT
      SUM(CASE WHEN subject_name IS NULL OR TRIM(subject_name) = '' THEN 1 ELSE 0 END) AS subject_name_nulls,
      SUM(CASE WHEN faculty_name IS NULL OR TRIM(faculty_name) = '' THEN 1 ELSE 0 END) AS faculty_name_nulls
    FROM timetable_entries
    WHERE section LIKE '%CSM%' OR branch_id IN (SELECT id FROM branches WHERE name LIKE '%CSM%')
""").fetchone()
print('nulls', dict(nulls) if nulls else None)
db.close()
