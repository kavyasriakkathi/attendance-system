from app import get_db

if __name__ == '__main__':
    db = get_db()
    records = db.execute('SELECT student_id, subject_id, status, date FROM attendance').fetchall()
    print('Attendance records:')
    for r in records:
        print(f'  Student {r["student_id"]} - Subject {r["subject_id"]} - {r["status"]} - {r["date"]}')
    db.close()