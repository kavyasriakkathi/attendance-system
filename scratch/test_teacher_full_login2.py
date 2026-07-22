from app import app
app.config['TESTING'] = True
client = app.test_client()

response = client.post('/teacher_login', data={
    'username': 'test_teacher999',
    'password': 'password123'
}, follow_redirects=True)

print("HTML:", response.data.decode()[:1500])
