import os
import datetime
import subprocess
import shutil

# Configuration
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
DATABASE_URL = os.environ.get("DATABASE_URL")
DATABASE_PATH = os.environ.get("DATABASE", "attendance.db")

def create_backup():
    """Create a backup of the current database (PostgreSQL or SQLite)."""
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
        
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    
    if DATABASE_URL and DATABASE_URL.startswith("postgres"):
        # PostgreSQL Backup
        backup_file = os.path.join(BACKUP_DIR, f"backup_postgres_{timestamp}.sql")
        print(f"Starting PostgreSQL backup to {backup_file}...")
        try:
            # Requires pg_dump to be installed on the system (Render has it)
            subprocess.run(["pg_dump", DATABASE_URL, "-f", backup_file], check=True)
            print("PostgreSQL backup completed successfully.")
        except Exception as e:
            print(f"Error during PostgreSQL backup: {e}")
    else:
        # SQLite Backup
        backup_file = os.path.join(BACKUP_DIR, f"backup_sqlite_{timestamp}.db")
        print(f"Starting SQLite backup to {backup_file}...")
        try:
            if os.path.exists(DATABASE_PATH):
                shutil.copy2(DATABASE_PATH, backup_file)
                print("SQLite backup completed successfully.")
            else:
                print(f"SQLite database file not found at {DATABASE_PATH}")
        except Exception as e:
            print(f"Error during SQLite backup: {e}")
            
    # Cleanup old backups (keep last 10)
    cleanup_old_backups()

def cleanup_old_backups(keep_count=10):
    """Keep only the most recent 'keep_count' backup files."""
    try:
        files = [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR) if os.path.isfile(os.path.join(BACKUP_DIR, f))]
        files.sort(key=os.path.getmtime, reverse=True)
        
        if len(files) > keep_count:
            for file_to_delete in files[keep_count:]:
                os.remove(file_to_delete)
                print(f"Deleted old backup: {file_to_delete}")
    except Exception as e:
        print(f"Error cleaning up old backups: {e}")

if __name__ == "__main__":
    print(f"[{datetime.datetime.now()}] Backup process started.")
    create_backup()
    print(f"[{datetime.datetime.now()}] Backup process finished.")
