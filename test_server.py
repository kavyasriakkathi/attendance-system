import requests

try:
    res = requests.post("http://localhost:5000/teacher_login", data={"username": "teacher1", "password": "password"})
    print(res.status_code)
except Exception as e:
    print(e)
