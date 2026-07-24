from pathlib import Path
import pytest
import hmac
import hashlib
import json
import app as app_module

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_key_12345")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "rzp_test_secret_67890")

    app_module.RAZORPAY_KEY_ID = "rzp_test_key_12345"
    app_module.RAZORPAY_KEY_SECRET = "rzp_test_secret_67890"

    db_path = Path(tmp_path) / "test_razorpay_fee.db"
    app_module.app.config.update(
        TESTING=True,
        DATABASE=str(db_path),
        SECRET_KEY="test-secret-key",
    )

    app_module._DB_INIT_DONE = False

    db = app_module.get_db()
    try:
        app_module.init_db(db)
        app_module._ensure_fee_schema(db)
        app_module._ensure_parent_schema(db)
        app_module._ensure_payment_transaction_schema(db)

        # Setup test branch and student
        db.execute("INSERT OR REPLACE INTO branches (id, name, location) VALUES (100, 'CSE-TEST', 'Building A')")
        db.execute("INSERT OR REPLACE INTO students (id, name, enrollment, branch_id, email) VALUES (500, 'Razorpay Student', 'ENR-RZP-500', 100, 'student@test.com')")

        # Setup test fee structure
        db.execute("INSERT OR REPLACE INTO fee_structures (id, fee_name, category, amount, academic_year, semester, due_date) VALUES (1, 'Tuition Fee 2026', 'Tuition', 50000.0, '2025-2026', 'Sem 1', '2026-12-31')")
        
        # Setup student fee assignment (Unpaid)
        db.execute("INSERT OR REPLACE INTO student_fee_assignments (id, student_id, fee_structure_id, discount_amount, status) VALUES (10, 500, 1, 5000.0, 'Unpaid')")
        
        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass

    with app_module.app.test_client() as c:
        yield c


def test_order_creation_and_successful_payment_flow(client):
    """Test full payment lifecycle: order creation, signature verification, fee payment, assignment recalculation, and notifications."""
    with client.session_transaction() as sess:
        sess['user_id'] = 500
        sess['student_id'] = 500
        sess['username'] = 'student500'
        sess['role'] = 'student'

    # 1. Create Razorpay Payment Order
    create_res = client.post('/api/payment/create-order', json={
        'assignment_id': 10,
        'amount': 45000.0
    })
    assert create_res.status_code == 200
    c_data = create_res.get_json()
    assert c_data['status'] == 'success'
    assert c_data['amount'] == 4500000 # 45,000 INR in paise
    order_id = c_data['order_id']
    assert order_id is not None

    # 2. Verify Valid Razorpay Signature
    payment_id = "pay_rzp_test_9999"
    # Generate valid signature matching secret
    generated_sig = hmac.new(
        b"rzp_test_secret_67890",
        f"{order_id}|{payment_id}".encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    verify_res = client.post('/api/payment/verify', json={
        'razorpay_order_id': order_id,
        'razorpay_payment_id': payment_id,
        'razorpay_signature': generated_sig,
        'assignment_id': 10
    })
    assert verify_res.status_code == 200
    v_data = verify_res.get_json()
    assert v_data['status'] == 'success'
    assert 'receipt_id' in v_data
    receipt_id = v_data['receipt_id']

    # 3. Verify Database Updates
    db = app_module.get_db()
    try:
        # Check student_fee_assignments status is updated to 'Paid'
        asg = db.execute("SELECT status FROM student_fee_assignments WHERE id = 10").fetchone()
        assert asg['status'] == 'Paid'

        # Check fee_payments record created
        pay = db.execute("SELECT * FROM fee_payments WHERE id = ?", (receipt_id,)).fetchone()
        assert pay is not None
        assert pay['amount_paid'] == 45000.0
        assert pay['payment_mode'] == 'Online (Razorpay)'
        assert pay['transaction_id'] == payment_id

        # Check payment_transactions table status
        tx = db.execute("SELECT * FROM payment_transactions WHERE razorpay_order_id = ?", (order_id,)).fetchone()
        assert tx['status'] == 'paid'
        assert tx['razorpay_payment_id'] == payment_id

        # Check system notifications generated
        notifs = db.execute("SELECT * FROM sys_notifications WHERE title LIKE '%Fee Payment%'").fetchall()
        assert len(notifs) >= 1
    finally:
        db.close()


def test_failed_payment_and_signature_rejection(client):
    """Test signature mismatch rejection and payment failure handling."""
    with client.session_transaction() as sess:
        sess['user_id'] = 500
        sess['student_id'] = 500
        sess['role'] = 'student'

    # Create Order
    create_res = client.post('/api/payment/create-order', json={
        'assignment_id': 10,
        'amount': 20000.0
    })
    order_id = create_res.get_json()['order_id']

    # Send Invalid Signature
    verify_res = client.post('/api/payment/verify', json={
        'razorpay_order_id': order_id,
        'razorpay_payment_id': "pay_fake_0000",
        'razorpay_signature': "invalid_bogus_signature_hash",
        'assignment_id': 10
    })
    assert verify_res.status_code == 400
    v_data = verify_res.get_json()
    assert v_data['status'] == 'error'
    assert 'Invalid Razorpay payment signature' in v_data['message']

    # Notify Failure Endpoint
    fail_res = client.post('/api/payment/failure', json={
        'razorpay_order_id': order_id,
        'error_code': 'PAYMENT_CANCELLED',
        'error_description': 'User closed razorpay modal'
    })
    assert fail_res.status_code == 200

    # Verify DB transaction status is failed
    db = app_module.get_db()
    try:
        tx = db.execute("SELECT * FROM payment_transactions WHERE razorpay_order_id = ?", (order_id,)).fetchone()
        assert tx['status'] == 'failed'
    finally:
        db.close()


def test_duplicate_payment_prevention_idempotency(client):
    """Test idempotency: processing the same Razorpay order twice does not duplicate payments."""
    with client.session_transaction() as sess:
        sess['user_id'] = 500
        sess['student_id'] = 500
        sess['role'] = 'student'

    # 1. First Payment
    c_res = client.post('/api/payment/create-order', json={'assignment_id': 10, 'amount': 45000.0})
    order_id = c_res.get_json()['order_id']

    payment_id = "pay_dup_test_101"
    sig = hmac.new(b"rzp_test_secret_67890", f"{order_id}|{payment_id}".encode('utf-8'), hashlib.sha256).hexdigest()

    v1 = client.post('/api/payment/verify', json={
        'razorpay_order_id': order_id,
        'razorpay_payment_id': payment_id,
        'razorpay_signature': sig,
        'assignment_id': 10
    })
    assert v1.status_code == 200
    pid1 = v1.get_json()['receipt_id']

    # 2. Duplicate Payment Call with same order_id and payment_id
    v2 = client.post('/api/payment/verify', json={
        'razorpay_order_id': order_id,
        'razorpay_payment_id': payment_id,
        'razorpay_signature': sig,
        'assignment_id': 10
    })
    assert v2.status_code == 200
    v2_data = v2.get_json()
    assert v2_data['status'] == 'success'
    assert v2_data['receipt_id'] == pid1

    # Verify only 1 fee_payments record exists
    db = app_module.get_db()
    try:
        pays = db.execute("SELECT * FROM fee_payments WHERE transaction_id = ?", (payment_id,)).fetchall()
        assert len(pays) == 1
    finally:
        db.close()


def test_pdf_and_html_receipt_generation(client):
    """Test HTML and ReportLab PDF receipt endpoints."""
    with client.session_transaction() as sess:
        sess['user_id'] = 500
        sess['student_id'] = 500
        sess['role'] = 'student'

    # Make a payment first
    c_res = client.post('/api/payment/create-order', json={'assignment_id': 10, 'amount': 45000.0})
    order_id = c_res.get_json()['order_id']
    payment_id = "pay_pdf_receipt_101"
    sig = hmac.new(b"rzp_test_secret_67890", f"{order_id}|{payment_id}".encode('utf-8'), hashlib.sha256).hexdigest()
    v = client.post('/api/payment/verify', json={
        'razorpay_order_id': order_id,
        'razorpay_payment_id': payment_id,
        'razorpay_signature': sig,
        'assignment_id': 10
    })
    rec_id = v.get_json()['receipt_id']

    # Test HTML Receipt Route
    html_res = client.get(f'/fee/receipt/{rec_id}')
    assert html_res.status_code == 200
    assert b'FEE RECEIPT' in html_res.data or b'Receipt' in html_res.data

    # Test PDF Receipt Route
    pdf_res = client.get(f'/fee/receipt/{rec_id}/pdf')
    assert pdf_res.status_code == 200
    assert pdf_res.mimetype == 'application/pdf'
    assert pdf_res.data.startswith(b'%PDF')


def test_admin_online_payments_dashboard_and_export(client):
    """Test Admin Online Payments dashboard, search, CSV export, and PDF export."""
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
        sess['role'] = 'admin'

    # Admin Management View
    res = client.get('/admin/fees/online-payments')
    assert res.status_code == 200
    assert b'Online Fee Payments' in res.data

    # Admin CSV Export
    csv_res = client.get('/admin/fees/online-payments/export-csv')
    assert csv_res.status_code == 200
    assert csv_res.mimetype == 'text/csv'

    # Admin PDF Export
    pdf_res = client.get('/admin/fees/online-payments/export-pdf')
    assert pdf_res.status_code == 200
    assert pdf_res.mimetype == 'application/pdf'
