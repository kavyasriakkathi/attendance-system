MailHog (local SMTP for dev)

Quick steps to run MailHog with Docker:

1. Start MailHog:

   PowerShell:
   ```powershell
   .\run_mailhog.ps1
   ```

   Or directly with Docker:
   ```powershell
   docker run --rm -p 1025:1025 -p 8025:8025 --name mailhog mailhog/mailhog
   ```

2. Update environment for local testing (create a copy of `.env.dev` or set these vars):

   ```text
   MAIL_SERVER=localhost
   MAIL_PORT=1025
   MAIL_DEV_FALLBACK=True
   MAIL_USERNAME=
   MAIL_PASSWORD=
   MAIL_USE_TLS=False
   MAIL_FROM=dev@example.local
   ```

3. Run the test script to send a message (after MailHog is running):

   ```powershell
   "c:/Users/kavya/OneDrive/Desktop/project 1/.venv/Scripts/python.exe" scratch/test_email.py
   ```

4. Open MailHog web UI at http://localhost:8025 to view captured messages.

Notes:
- MailHog does not require authentication; the app will use the development fallback if credentials are not set or `MAIL_DEV_FALLBACK` is `True`.
- If you prefer not to use Docker, download the MailHog binary from the project releases and run it locally.
