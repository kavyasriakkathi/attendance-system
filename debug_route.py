from app import app

if __name__ == '__main__':
    with app.test_client() as c:
        resp = c.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=True)
        resp = c.get('/attendance?branch_id=1')
        print('Status:', resp.status_code)
        data = resp.data.decode()
        print('Subjects in response:', 'PYTHON' in data, 'ODEVC' in data)
        # Print the subject options
        import re
        options = re.findall(r'<option[^>]*>.*?</option>', data)
        print('Subject options found:')
        for opt in options:
            if 'PYTHON' in opt or 'ODEVC' in opt:
                print(' ', opt)