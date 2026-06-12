import os
os.environ["DATABASE_URL"] = "postgresql://mock_user:mock_pass@localhost:5432/mock_db"

from app import app, get_db
import psycopg2

print("Initializing Flask app config with PostgreSQL...")
app.config["DATABASE"] = os.environ["DATABASE_URL"]

try:
    print("Calling get_db()...")
    db = get_db()
except psycopg2.OperationalError as e:
    print("Expected psycopg2.OperationalError caught at top level.")
except Exception as e:
    print("Caught unexpected exception at top level:", repr(e))
