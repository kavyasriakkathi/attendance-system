import smtplib
import ssl
from email.message import EmailMessage
import os

from dotenv import load_dotenv
load_dotenv()

def test_email():
    mail_server = "smtp.gmail.com"
    mail_port = 587
    mail_username = os.environ.get("MAIL_USERNAME")
    mail_password = os.environ.get("MAIL_PASSWORD")
    
    print(f"Testing with {mail_username} / {mail_password}")
    
    msg = EmailMessage()
    msg["Subject"] = "Test Email"
    msg["From"] = mail_username
    msg["To"] = mail_username
    msg.set_content("This is a test email")
    
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(mail_server, mail_port, timeout=10) as server:
            server.set_debuglevel(1)
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(mail_username, mail_password)
            server.send_message(msg)
            print("Email sent successfully!")
    except Exception as e:
        print(f"Failed to send email: {e}")

if __name__ == "__main__":
    test_email()
