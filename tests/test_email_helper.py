import os
import unittest
from unittest import mock

import smtplib

from app import send_email_gmail


class EmailHelperTests(unittest.TestCase):
    @mock.patch('socket.create_connection')
    @mock.patch('smtplib.SMTP')
    def test_send_email_success(self, mock_smtp_cls, mock_create_conn):
        # socket connection ok
        mock_create_conn.return_value = mock.Mock()
        mock_smtp = mock.Mock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        # Ensure env vars
        os.environ['MAIL_USERNAME'] = 'user@example.com'
        os.environ['MAIL_PASSWORD'] = 'app password with spaces'

        ok, err = send_email_gmail('subj', 'to@example.com', 'body', debug=False)
        self.assertTrue(ok)
        self.assertIsNone(err)
        mock_smtp.starttls.assert_called()
        # login called with password stripped of spaces
        called_user, called_pass = mock_smtp.login.call_args[0]
        self.assertEqual(called_user, 'user@example.com')
        self.assertEqual(called_pass, 'apppasswordwithspaces')

    @mock.patch('socket.create_connection', side_effect=OSError('Network is unreachable'))
    def test_no_internet(self, mock_create_conn):
        os.environ['MAIL_USERNAME'] = 'user@example.com'
        os.environ['MAIL_PASSWORD'] = 'pwd'
        ok, err = send_email_gmail('s', 't', 'b')
        self.assertFalse(ok)
        self.assertIn('No internet', err)

    @mock.patch('socket.create_connection')
    @mock.patch('smtplib.SMTP')
    def test_auth_failure(self, mock_smtp_cls, mock_create_conn):
        mock_create_conn.return_value = mock.Mock()
        mock_smtp = mock.Mock()
        mock_smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b'5.7.8 Authentication failed')
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        os.environ['MAIL_USERNAME'] = 'user@example.com'
        os.environ['MAIL_PASSWORD'] = 'pwd'

        ok, err = send_email_gmail('s', 't', 'b')
        self.assertFalse(ok)
        self.assertIn('SMTP authentication failed', err)


if __name__ == '__main__':
    unittest.main()
