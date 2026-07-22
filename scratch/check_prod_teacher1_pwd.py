import psycopg2
import psycopg2.extras
from werkzeug.security import check_password_hash

PROD_DB_URL = "postgresql://neondb_owner:npg_tlI7cGRBogs1@ep-withered-math-apo99psx-pooler.c-7.us-east-1.aws.neon.tech/neondb?sslmode=require"

conn = psycopg2.connect(PROD_DB_URL)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute("SELECT id, username, password FROM users WHERE username = 'teacher1';")
user_row = cur.fetchone()
print("User row in production Neon database:", dict(user_row) if user_row else None)

if user_row:
    pwd_in_db = user_row["password"]
    print("Testing password '1234':", check_password_hash(pwd_in_db, "1234"))
    print("Testing password 'teacher123':", check_password_hash(pwd_in_db, "teacher123"))

cur.close()
conn.close()
