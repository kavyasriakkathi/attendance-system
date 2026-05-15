import sys
import traceback

try:
    import app
    app_obj = app.app
    with app_obj.test_client() as c:
        resp = c.get('/dashboard')
        print('STATUS:', resp.status_code)
        data = resp.get_data(as_text=True)
        print('\n--- RESPONSE BEGIN ---\n')
        print(data[:4000])
        print('\n--- RESPONSE END ---\n')
except Exception as e:
    print('IMPORT/REQUEST ERROR:', type(e).__name__, str(e))
    traceback.print_exc()
    sys.exit(2)
