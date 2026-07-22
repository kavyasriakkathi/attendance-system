import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    # Try loading from .env
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    DATABASE_URL = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found.")
    exit(1)

conn = psycopg2.connect(DATABASE_URL, sslmode='require')
cur = conn.cursor()

# Show all branches first
cur.execute("SELECT id, name FROM branches ORDER BY name;")
rows = cur.fetchall()
print("Current branches:")
for r in rows:
    print(f"  id={r[0]}, name={r[1]}")

# Rename MECH -> CSW
cur.execute("UPDATE branches SET name = 'CSW' WHERE UPPER(name) = 'MECH';")
affected = cur.rowcount
conn.commit()
print(f"\nUpdated {affected} branch row(s): MECH -> CSW")

# Verify
cur.execute("SELECT id, name FROM branches ORDER BY name;")
rows = cur.fetchall()
print("\nBranches after update:")
for r in rows:
    print(f"  id={r[0]}, name={r[1]}")

cur.close()
conn.close()
