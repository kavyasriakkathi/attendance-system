import sys
import os
import uuid
import unittest

sys.path.insert(0, r"c:\Users\kavya\OneDrive\Desktop\project 1")

from app import app, init_db, get_db, calculate_grade

class UniversityResultsModuleTestCase(unittest.TestCase):

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test_secret_key'
        self.db_path = os.path.join(os.path.dirname(__file__), f'test_uni_results_{uuid.uuid4().hex[:8]}.db')
        app.config['DATABASE'] = self.db_path
        self.client = app.test_client()

        with app.app_context():
            init_db()
            db = get_db()
            
            # Seed branch
            db.execute("INSERT INTO branches (name) VALUES ('CSM')")
            branch_id = db.execute("SELECT id FROM branches WHERE name = 'CSM'").fetchone()[0]

            # Seed subject
            db.execute("INSERT INTO subjects (name, branch_id) VALUES ('Mathematics-I', ?)", (branch_id,))
            subject_id = db.execute("SELECT id FROM subjects WHERE name = 'Mathematics-I'").fetchone()[0]

            # Seed student
            db.execute(
                "INSERT INTO students (name, enrollment, branch_id) VALUES ('University Student', '21CSM001', ?)",
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

    def test_grade_calculation_scale(self):
        self.assertEqual(calculate_grade(95, 100)['grade'], 'A+')
        self.assertEqual(calculate_grade(95, 100)['gp'], 10)
        self.assertEqual(calculate_grade(85, 100)['grade'], 'A')
        self.assertEqual(calculate_grade(75, 100)['grade'], 'B')
        self.assertEqual(calculate_grade(65, 100)['grade'], 'C')
        self.assertEqual(calculate_grade(55, 100)['grade'], 'D')
        self.assertEqual(calculate_grade(45, 100)['grade'], 'Fail')

    def test_admin_university_exam_creation_and_deletion(self):
        with self.client.session_transaction() as sess:
            sess['username'] = 'admin'
            sess['role'] = 'admin'

        res = self.client.post('/admin/exams', data={
            'action': 'create',
            'exam_name': 'Mid 1 Internal Exam',
            'exam_type': 'internal_1',
            'academic_year': '2025-2026',
            'semester': 'Semester 1',
            'branch_id': '1',
            'section': 'A'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Examination created successfully', res.data)

        # Verify DB entry
        with app.app_context():
            db = get_db()
            exam = db.execute("SELECT * FROM exams WHERE exam_name = 'Mid 1 Internal Exam'").fetchone()
            self.assertIsNotNone(exam)
            self.assertEqual(exam['exam_type'], 'internal_1')
            self.assertEqual(exam['semester'], 'Semester 1')

    def test_teacher_university_marks_entry(self):
        # Create exam first
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO exams (exam_name, exam_type, academic_year, semester, branch_id, section) VALUES ('Semester End Exam', 'external', '2025-2026', 'Semester 1', 1, 'A')")
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
            'mid1_marks[]': ['25.0'],
            'mid2_marks[]': ['28.0'],
            'mid3_marks[]': [''],
            'external_marks[]': ['60.0'],
            'marks_obtained[]': ['88.0']
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'saved successfully', res.data)

        # Check DB values
        with app.app_context():
            db = get_db()
            mark = db.execute("SELECT mid1_marks, mid2_marks, external_marks, marks_obtained FROM marks WHERE student_id = 1 AND subject_id = 1 AND exam_id = 1").fetchone()
            self.assertIsNotNone(mark)
            self.assertEqual(float(mark['mid1_marks']), 25.0)
            self.assertEqual(float(mark['mid2_marks']), 28.0)
            self.assertEqual(float(mark['external_marks']), 60.0)
            self.assertEqual(float(mark['marks_obtained']), 88.0)

    def test_student_semester_wise_results(self):
        with app.app_context():
            db = get_db()
            # Exam for Semester 1
            db.execute("INSERT INTO exams (exam_name, exam_type, academic_year, semester, branch_id, section) VALUES ('Semester 1 Final Exam', 'external', '2025-2026', 'Semester 1', 1, 'A')")
            db.execute("INSERT INTO marks (student_id, subject_id, exam_id, mid1_marks, mid2_marks, external_marks, marks_obtained, max_marks, entered_by_teacher) VALUES (1, 1, 1, 25.0, 28.0, 60.0, 88.0, 100, 1)")
            db.commit()

        with self.client.session_transaction() as sess:
            sess['username'] = '21CSM001'
            sess['student_id'] = 1
            sess['role'] = 'student'

        res = self.client.get('/student/results')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Semester 1', res.data)
        self.assertIn(b'Mathematics-I', res.data)
        self.assertIn(b'88.0', res.data)
        self.assertIn(b'CGPA', res.data)

    def test_admin_university_results_dashboard(self):
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO exams (exam_name, exam_type, academic_year, semester, branch_id, section) VALUES ('Semester 1 Final Exam', 'external', '2025-2026', 'Semester 1', 1, 'A')")
            db.execute("INSERT INTO marks (student_id, subject_id, exam_id, mid1_marks, mid2_marks, external_marks, marks_obtained, max_marks, entered_by_teacher) VALUES (1, 1, 1, 25.0, 28.0, 60.0, 88.0, 100, 1)")
            db.commit()

        with self.client.session_transaction() as sess:
            sess['username'] = 'admin'
            sess['role'] = 'admin'

        res = self.client.get('/admin/results?semester=Semester+1')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Class Average', res.data)
        self.assertIn(b'88.0', res.data)
        self.assertIn(b'Semester 1', res.data)

if __name__ == '__main__':
    unittest.main()
