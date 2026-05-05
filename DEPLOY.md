Deploying to Render — SMTP setup

This document explains how to configure SMTP for production on Render so your deployment (https://attendance-system-gi39.onrender.com) can send email.

1. Add environment variables in Render

   In the Render dashboard, go to Services → attendance-system → Environment (or Environment Variables) and add the following keys as **secrets**:

   - MAIL_USERNAME : your Gmail address (e.g., instituteattendanceapp@gmail.com)
   - MAIL_PASSWORD : Gmail App Password (16 characters, no spaces)
   - MAIL_SERVER : smtp.gmail.com
   - MAIL_PORT : 587
   - MAIL_USE_TLS : True
   - MAIL_FROM : same as MAIL_USERNAME or a preferred sender
   - MAIL_DEV_FALLBACK : False

2. Create a Gmail App Password (recommended)

   - Enable 2-Step Verification for the Gmail account: https://myaccount.google.com/security
   - Under Security → App passwords, create a new App Password (choose Other and name it `attendance-app`).
   - Copy the 16-character password (no spaces) and paste it into Render as `MAIL_PASSWORD`.

3. Redeploy the service

   - After saving environment variables, trigger a manual deploy in the Render UI (Service → Manual Deploy / Trigger Deploy) so the new variables are available to the running service.

4. Verify SMTP from the app

   - The app exposes an SMTP check route: `/admin/check-smtp` (see the handler in [app.py](app.py#L1383)). Open:
     https://attendance-system-gi39.onrender.com/admin/check-smtp

   - Check Render logs: Render Dashboard → Service → Logs for SMTP connection/auth messages.

5. Troubleshooting

   - 535 BadCredentials: ensure `MAIL_USERNAME` matches the Google account that generated the App Password, and `MAIL_PASSWORD` is the 16-character App Password copied exactly (no spaces).
   - Network unreachable / errno 101: verify Render allows outbound SMTP to the provider; if blocked, consider using a transactional email provider (SendGrid, Mailgun) and update the app to use their SMTP/API.
   - Port/SSL: to use SSL on port 465, set `MAIL_PORT=465` and `MAIL_USE_TLS=False`.

6. Local testing (optional)

   - For development, you can test without sending real mail using MailHog. See `MAILHOG.md` and `.env.dev` for a local testing configuration.

Files referenced in repo:
- [render.yaml](render.yaml#L1)
- [app.py SMTP check route](app.py#L1383)
- [MAILHOG.md](MAILHOG.md)

If you want, I can also prepare a Render Environment screenshot template or add a CI step to ensure SMTP vars are present before deploying.