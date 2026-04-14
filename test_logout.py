from app import app

if __name__ == '__main__':
    with app.test_client() as c:
        resp = c.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=True)
        print('Login status:', resp.status_code)
        resp = c.get('/logout')
        print('Logout status:', resp.status_code)
        print('Logout response location:', resp.headers.get('Location'))
        resp = c.get('/logout', follow_redirects=True)
        print('Logout with follow status:', resp.status_code)
        print('Final URL:', resp.request.url)
        print('Contains login form:', 'username' in resp.data.decode())