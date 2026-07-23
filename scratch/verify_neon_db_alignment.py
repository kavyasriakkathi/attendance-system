import os
import requests
import json
import psycopg2
import psycopg2.extras
from urllib.parse import urlparse

RENDER_BASE_URL = "https://attendance-system-gi39.onrender.com"
SEED_SCRIPT_URL = os.environ.get("DATABASE_URL", "")

def mask_url(url_str):
    try:
        parsed = urlparse(url_str)
        # Reconstruct with masked password
        netloc = parsed.hostname
        if parsed.port:
            netloc += f":{parsed.port}"
        if parsed.username:
            netloc = f"{parsed.username}:*****@{netloc}"
        return f"{parsed.scheme}://{netloc}{parsed.path}?{parsed.query}"
    except Exception:
        return url_str

def main():
    # 1. Fetch Render's actual DATABASE_URL from /admin/check-db
    session = requests.Session()
    session.post(f"{RENDER_BASE_URL}/login", data={"username": "admin", "password": "admin123"})
    res = session.get(f"{RENDER_BASE_URL}/admin/check-db")
    render_data = res.json()
    render_raw_url = render_data.get("database", "")

    render_masked = mask_url(render_raw_url)
    seed_masked = mask_url(SEED_SCRIPT_URL)

    render_parsed = urlparse(render_raw_url)
    seed_parsed = urlparse(SEED_SCRIPT_URL)

    render_host_db = f"Host: {render_parsed.hostname}, DB: {render_parsed.path.lstrip('/')}"
    seed_host_db = f"Host: {seed_parsed.hostname}, DB: {seed_parsed.path.lstrip('/')}"

    are_same = (
        render_parsed.hostname == seed_parsed.hostname and
        render_parsed.path == seed_parsed.path and
        render_parsed.port == seed_parsed.port
    )

    print("==========================================")
    print("1. RENDER DATABASE_URL")
    print("==========================================")
    print("Render Masked URL:", render_masked)
    print(render_host_db)

    print("\n==========================================")
    print("2. SEEDING SCRIPT DATABASE_URL")
    print("==========================================")
    print("Seeding Script Masked URL:", seed_masked)
    print(seed_host_db)

    print("\n==========================================")
    print("3. EXACT MATCH CONFIRMATION")
    print("==========================================")
    print(f"Are Render DB and Seeding Script DB exactly the same? -> {are_same}")

    print("\n==========================================")
    print("4. EXACT DATABASE QUERY RESULTS")
    print("==========================================")
    conn = psycopg2.connect(render_raw_url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("--- QUERY 1: SELECT COUNT(*) FROM teachers; ---")
    cur.execute("SELECT COUNT(*) FROM teachers;")
    cnt_result = cur.fetchone()
    print(dict(cnt_result))

    print("\n--- QUERY 2: SELECT id, username, name FROM teachers ORDER BY id; ---")
    cur.execute("SELECT id, username, name FROM teachers ORDER BY id;")
    rows = cur.fetchall()
    for r in rows:
        print(dict(r))

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
