#!/usr/bin/env python3
"""CLI runner to send a test email using the app's email helper.

Usage:
    python test_email.py recipient@example.com
"""
import sys
from app import send_email_with_error


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_email.py recipient@example.com")
        sys.exit(2)
    recipient = sys.argv[1]
    subject = "Test email from Attendance App"
    body = "This is a test email sent by test_email.py"

    ok, err = send_email_with_error(subject, recipient, body)
    if ok:
        print("Test email sent successfully to", recipient)
        sys.exit(0)
    else:
        print("Failed to send test email:", err)
        sys.exit(1)


if __name__ == '__main__':
    main()
