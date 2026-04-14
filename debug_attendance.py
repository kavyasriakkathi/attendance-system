from app import app, init_db, get_db

if __name__ == '__main__':
    init_db()
    db = get_db()
    db.execute("INSERT OR IGNORE INTO branches (id,name,location) VALUES (1, 'TestBranch', 'loc')")
    db.execute("INSERT OR IGNORE INTO subjects (id,name,branch_id) VALUES (1, 'Math', 1)")
    db.execute("INSERT OR IGNORE INTO students (id,name,enrollment,branch_id,email) VALUES (1, 'Test Student', 'E001', 1, 'test@example.com')")
    db.commit()
    db.close()

    with app.test_client() as client:
        resp = client.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=True)
        print('login status', resp.status_code)
        resp = client.post('/attendance', data={'branch_id': '1', 'subject_id': '1', 'date': '2026-04-13', 'student_id': '1', 'status_1': 'Present', 'note_1': 'ok'}, follow_redirects=True)
        print('attendance post status', resp.status_code)
        print('flash success', b'Attendance recorded successfully' in resp.data)
        db = get_db()
        row = db.execute('SELECT student_id, status, note, date FROM attendance WHERE student_id = 1 AND subject_id = 1').fetchone()
        print('db row', row)
        db.close()
