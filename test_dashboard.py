from app import app

if __name__ == '__main__':
    with app.test_client() as c:
        resp = c.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=True)
        data = resp.data.decode()
        print('Status:', resp.status_code)
        # Look for overall percentage in dashboard
        if 'Overall Attendance' in data:
            print('Overall attendance section found')
            start = data.find('Overall Attendance')
            end = data.find('</div>', start + 100)
            if end != -1:
                print('Dashboard stats:', data[start:end+6])
        else:
            print('Overall attendance section NOT found')