import requests
import sqlite3
from werkzeug.security import generate_password_hash

conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.execute("INSERT OR IGNORE INTO teachers (id, name, username, password, branch_id) VALUES (999, 'TT', 'tt2', 'tt2', 1)")
cursor.execute("INSERT OR IGNORE INTO users (id, username, password, role) VALUES (999, 'tt2', ?, 'teacher')", (generate_password_hash('password'),))
conn.commit()
conn.close()

session = requests.Session()
res = session.post('http://127.0.0.1:5001/teacher_login', data={'username':'tt2', 'password':'password'})
print('Login Status:', res.status_code)
if res.status_code == 500:
    print('500 Error Data:', res.text[:1500])
else:
    print('URL:', res.url)
    
res = session.get('http://127.0.0.1:5001/teacher/dashboard')
print('Teacher Dashboard Status:', res.status_code)
if res.status_code == 500:
    print('500 Error Data:', res.text[:1500])
