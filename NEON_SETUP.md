# Neon PostgreSQL Setup Guide
## (Replacing Render's removed free PostgreSQL)

---

## Step 1 — Create a Free Neon Account

1. Open **https://neon.tech** in your browser
2. Click **Sign Up** → use your GitHub or Google account (it's free)
3. After login, click **New Project**
4. Fill in:
   - **Project name:** `attendance-system`
   - **Database name:** `attendance`
   - **Region:** `AWS ap-south-1` (Mumbai) — closest for India
5. Click **Create Project**

---

## Step 2 — Copy Your Connection String

After the project is created:

1. On the project dashboard, click **Connection Details**
2. Under **Connection string**, select the **psycopg2** tab (or copy the full URI)
3. It will look like:
   ```
   postgresql://attendance_owner:YOURPASSWORD@ep-cool-name-123456.ap-south-1.aws.neon.tech/attendance?sslmode=require
   ```
4. **Copy this entire string** — you'll need it in Steps 3 and 4

---

## Step 3 — Migrate Your Local Data to Neon

Run this in your project folder (PowerShell):

```powershell
cd "c:\Users\kavya\OneDrive\Desktop\project 1"

# Set your Neon URL (replace with your actual URL)
$env:DATABASE_URL="postgresql://attendance_owner:YOURPASSWORD@ep-xxx.ap-south-1.aws.neon.tech/attendance?sslmode=require"

# First deploy the app once so tables are created, then run this:
python migrate_sqlite_to_postgres.py
```

> **Note:** The migration script requires the tables to exist first.
> Deploy to Render first (Step 4), then run the migration.

---

## Step 4 — Set DATABASE_URL on Render

1. Go to **https://dashboard.render.com**
2. Click your **attendance-system** web service
3. Click the **Environment** tab (left sidebar)
4. Find `DATABASE_URL` → click **Edit** (or **Add** if missing)
5. Paste your **Neon connection string** from Step 2
6. Click **Save Changes**
7. Render will automatically redeploy

---

## Step 5 — Verify the Deployment

After Render finishes deploying (~2 min):

1. Open your app URL (e.g. `https://attendance-system.onrender.com`)
2. Try logging in as admin
3. Check the dashboard — it should now show **"Neon PostgreSQL"** as storage
4. Optionally visit `/admin/check-db` to verify row counts

---

## What Changed in the Code

| File | Change |
|------|--------|
| `render.yaml` | Removed `databases:` block (Render free PG), added `DATABASE_URL: sync: false` |
| `Procfile` | Changed `python app.py` → `gunicorn wsgi:app` (production server) |
| `app.py` | Added `_normalize_db_url()` to auto-fix `postgres://` → `postgresql://` and add `sslmode=require` |
| `app.py` | Dashboard now shows **"Neon PostgreSQL"** instead of generic "PostgreSQL" |
| `migrate_sqlite_to_postgres.py` | Updated messages to reference Neon |

---

## Troubleshooting

### "SSL connection error"
Your connection string is missing `?sslmode=require`. The code adds it automatically,
but double-check the URL you pasted in Render has no typos.

### "Table does not exist"
The app initializes tables on first request. After deploy, hit your app URL once
and the tables will be created automatically via `init_db()`.

### "connection refused" / timeout
- Check the Neon dashboard — your project may be in "suspend" mode (free tier auto-suspends after 5 min idle)
- First request after idle will take ~3-5 seconds to wake up — this is normal for Neon free tier

### "psycopg2 import failed"
Ensure `psycopg2-binary` is in `requirements.txt`. It already is in your project.

---

## Free Tier Limits (Neon)

| Limit | Value |
|-------|-------|
| Storage | 512 MB |
| Compute hours | 191.9 hrs/month |
| Databases | 1 |
| Auto-suspend after idle | 5 minutes |
| SSL | Required (already handled) |

This is more than enough for a college attendance system.
