from app import get_db

if __name__ == '__main__':
    db = get_db()
    # Add some test attendance data
    db.execute("INSERT OR IGNORE INTO attendance (student_id, branch_id, subject_id, date, status, note) VALUES (5, 1, 1, '2026-04-13', 'Present', 'Good')")
    db.execute("INSERT OR IGNORE INTO attendance (student_id, branch_id, subject_id, date, status, note) VALUES (6, 1, 1, '2026-04-13', 'Absent', 'Sick')")
    db.execute("INSERT OR IGNORE INTO attendance (student_id, branch_id, subject_id, date, status, note) VALUES (5, 1, 4, '2026-04-13', 'Present', 'Active')")
    db.commit()
    print('Added test attendance data')
    db.close()