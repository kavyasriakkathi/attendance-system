import sys
import os
import uuid
import unittest

sys.path.insert(0, r"c:\Users\kavya\OneDrive\Desktop\project 1")

from app import app, init_db, get_db, calculate_grade

class ResultsModuleTestCase(unittest.TestCase):

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test_secret_key'
        self.db_path = os.path.join(os.path.dirname(__file__), f'test_results_{uuid.uuid4().hex[:8]}.db')
        app.config['DATABASE'] = self.db_path
        self.client = app.test_client()

        with app.app_context():
            init_db()
            db = get_db()
            
            # Seed branch
            db.execute("INSERT INTO branches (name) VALUES ('CSM')")
            branch_id = db.execute("SELECT id FROM branches WHERE name = 'CSM'").fetchone()[0]

            # Seed subject
            db.execute("INSERT INTO subjects (name, branch_id) VALUES ('Maths', ?)", (branch_id,))
            subject_id = db.execute("SELECT id FROM subjects WHERE name = 'Maths'").fetchone()[0]

            # Seed student
            db.execute(
                "INSERT INTO students (name, enrollment, branch_id) VALUES ('Test Student', '21CSM001', ?)",
                (branch_id,)
            )

            # Seed teacher user
            db.execute(
                "INSERT INTO users (username, password, role) VALUES ('testteacher', 'teacher123', 'teacher')"
            )

            db.commit()

    def tearDown(self):
        if hasattr(self, 'db_path') and os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_grade_calculation(self):
        self.assertEqual(calculate_grade(95, 100)['grade'], 'A+')
        self.assertEqual(calculate_grade(95, 100)['gp'], 10)
        self.assertEqual(calculate_grade(85, 100)['grade'], 'A')
        self.assertEqual(calculate_grade(75, 100)['grade'], 'B')
        self.assertEqual(calculate_grade(65, 100)['grade'], 'C')
        self.assertEqual(calculate_grade(55, 100)['grade'], 'D')
        self.assertEqual(calculate_grade(45, 100)['grade'], 'Fail')

    def test_admin_exam_creation_and_deletion(self):
        with self.client.session_transaction() as sess:
            sess['username'] = 'admin'
            sess['role'] = 'admin'

        res = self.client.post('/admin/exams', data={
            'action': 'create',
            'exam_name': 'Internal Exam 1',
            'academic_year': '2025-2026',
            'semester': '1',
            'branch_id': '1',
            'section': 'A'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Examination created successfully', res.data)

        # Check DB
        with app.app_context():
            db = get_db()
            exam = db.execute("SELECT * FROM exams WHERE exam_name = 'Internal Exam 1'").fetchone()
            self.assertIsNotNone(exam)

    def test_teacher_marks_entry(self):
        # Create exam first
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO exams (exam_name, academic_year, semester, branch_id, section) VALUES ('Internal Exam 1', '2025-2026', '1', 1, 'A')")
            db.commit()

        with self.client.session_transaction() as sess:
            sess['username'] = 'testteacher'
            sess['role'] = 'teacher'

        res = self.client.post('/teacher/enter-marks', data={
            'subject_id': '1',
            'exam_id': '1',
            'branch_id': '1',
            'section': 'A',
            'max_marks': '100',
            'student_id[]': ['1'],
            'marks_obtained[]': ['88.5']
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'saved successfully', res.data)

        # Check DB
        with app.app_context():
            db = get_db()
            mark = db.execute("SELECT marks_obtained FROM marks WHERE student_id = 1 AND subject_id = 1 AND exam_id = 1").fetchone()
            self.assertIsNotNone(mark)
            self.assertEqual(float(mark['marks_obtained']), 88.5)

    def test_student_results_and_analytics(self):
        # Seed exam and mark
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO exams (exam_name, academic_year, semester, branch_id, section) VALUES ('Internal Exam 1', '2025-2026', '1', 1, 'A')")
            db.execute("INSERT INTO marks (student_id, subject_id, exam_id, marks_obtained, max_marks, entered_by_teacher) VALUES (1, 1, 1, 88.5, 100, 1)")
            db.commit()

        with self.client.session_transaction() as sess:
            sess['username'] = '21CSM001'
            sess['student_id'] = 1
            sess['role'] = 'student'

        res = self.client.get('/student/results')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'88.5', res.data)
        self.assertIn(b'Maths', res.data)

    def test_admin_results_dashboard(self):
        # Seed exam and mark
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO exams (exam_name, academic_year, semester, branch_id, section) VALUES ('Internal Exam 1', '2025-2026', '1', 1, 'A')")
            db.execute("INSERT INTO marks (student_id, subject_id, exam_id, marks_obtained, max_marks, entered_by_teacher) VALUES (1, 1, 1, 88.5, 100, 1)")
            db.commit()

        with self.client.session_transaction() as sess:
            sess['username'] = 'admin'
            sess['role'] = 'admin'

        res = self.client.get('/admin/results')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Class Average', res.data)
        self.assertIn(b'88.5', res.data)

if __name__ == '__main__':
    unittest.main()
