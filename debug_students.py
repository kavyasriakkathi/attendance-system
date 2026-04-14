from app import app

if __name__ == '__main__':
    with app.test_client() as c:
        resp = c.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=True)
        
        # Test with only branch_id - should load subjects but not students
        resp = c.get('/attendance?branch_id=1')
        data = resp.data.decode()
        print('Branch only - subjects loaded:', 'PYTHON' in data)
        print('Branch only - students loaded:', 'student_id' in data and 'name="status_' in data)
        
        # Test with branch_id and subject_id - should load students
        resp = c.get('/attendance?branch_id=1&subject_id=1')
        data = resp.data.decode()
        print('Branch+Subject - subjects loaded:', 'PYTHON' in data)
        print('Branch+Subject - students loaded:', 'student_id' in data and 'name="status_' in data)