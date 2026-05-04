from app import app

with app.test_client() as client:
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
        sess['role'] = 'admin'

    resp = client.get('/admin/check-smtp')
    print(resp.status_code)
    print(resp.get_data(as_text=True))
