import sqlite3
from werkzeug.security import generate_password_hash

conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.execute("INSERT OR IGNORE INTO teachers (name, username, password, branch_id) VALUES ('Test Teacher', 'tt', 'tt', 1)")
cursor.execute("INSERT OR IGNORE INTO users (username, password, role) VALUES ('tt', ?, 'teacher')", (generate_password_hash('password'),))
conn.commit()
conn.close()

from app import app
app.testing = True
client = app.test_client()

print("Posting to /teacher_login...")
response = client.post('/teacher_login', data={'username': 'tt', 'password': 'password'})
print('Status:', response.status_code)
if response.status_code in (301, 302):
    loc = response.headers.get('Location')
    print('Redirecting to:', loc)
    response = client.get(loc)
    print('Redirect Status:', response.status_code)
    if response.status_code == 500:
        print("500 ERROR DATA:")
        print(response.data.decode()[:1500])
elif response.status_code == 500:
    print("500 ERROR DATA:")
    print(response.data.decode()[:1500])
else:
    print("Success?")
