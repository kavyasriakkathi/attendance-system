from app import app

if __name__ == '__main__':
    with app.test_client() as c:
        resp = c.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=True)
        resp = c.post('/reports', data={'branch_id': '1'}, follow_redirects=True)
        data = resp.data.decode()
        print('Status:', resp.status_code)
        # Look for stats section
        if 'Attendance Statistics' in data:
            print('Stats section found')
        else:
            print('Stats section NOT found')
        # Check for specific content
        if '85.7%' in data:
            print('Percentage found')
        else:
            print('Percentage NOT found')
        if 'John Doe' in data:
            print('Student name found')
        else:
            print('Student name NOT found')
        # Look for student stats section
        if 'Student Attendance Percentages' in data:
            print('Student stats section found')
        else:
            print('Student stats section NOT found')
        # Look for subject stats section
        if 'Subject Attendance Percentages' in data:
            print('Subject stats section found')
            start = data.find('<h2>Subject Attendance Percentages</h2>')
            end = data.find('</table>', start + 100)
            if end != -1:
                print('Subject stats table:', data[start:end+8])
        else:
            print('Subject stats section NOT found')