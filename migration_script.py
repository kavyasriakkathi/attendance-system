import os
import sqlite3
import sys

try:
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        print('ERROR: DATABASE_URL not set')
        sys.exit(1)

    import psycopg2
    import psycopg2.extras

    sqlite_path = os.path.join(os.getcwd(), 'attendance.db')
    if not os.path.exists(sqlite_path):
        print(f'ERROR: SQLite file not found at {sqlite_path}')
        sys.exit(1)

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg2.connect(db_url, sslmode='require', connect_timeout=10)
    pg_cur = pg_conn.cursor()

    tables = ['branches', 'students', 'subjects', 'attendance', 'timetable_entries', 'users']
    sqlite_counts = {}
    pg_counts_before = {}
    pg_counts_after = {}

    def map_type(sqlite_type):
        if not sqlite_type:
            return 'TEXT'
        t = sqlite_type.upper()
        if 'INT' in t:
            return 'BIGINT'
        if 'CHAR' in t or 'CLOB' in t or 'TEXT' in t:
            return 'TEXT'
        if 'BLOB' in t:
            return 'BYTEA'
        if 'REAL' in t or 'FLOA' in t or 'DOUB' in t:
            return 'DOUBLE PRECISION'
        if 'NUM' in t or 'DEC' in t:
            return 'NUMERIC'
        return 'TEXT'

    def table_exists(pg_cursor, table_name):
        pg_cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name=%s)",
            (table_name,)
        )
        return pg_cursor.fetchone()[0]

    def pg_columns(pg_cursor, table_name):
        pg_cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
            (table_name,)
        )
        return {row[0] for row in pg_cursor.fetchall()}

    def create_table(pg_cursor, table_name, columns):
        definitions = []
        pk_cols = []
        for col in columns:
            col_name = col['name']
            col_type = map_type(col['type'])
            definitions.append(f'"{col_name}" {col_type}')
            if col['pk']:
                pk_cols.append(col_name)
        if pk_cols:
            definitions.append('PRIMARY KEY (%s)' % ', '.join(f'"{c}"' for c in pk_cols))
        stmt = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(definitions)})'
        pg_cursor.execute(stmt)

    def add_missing_columns(pg_cursor, table_name, columns, existing):
        for col in columns:
            name = col['name']
            if name not in existing:
                col_type = map_type(col['type'])
                print(f'Adding missing column to PostgreSQL {table_name}: {name} {col_type}')
                pg_cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{name}" {col_type}')

    def set_sequence(pg_cursor, table_name, col_name='id'):
        pg_cursor.execute(
            "SELECT column_default FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
            (table_name, col_name)
        )
        result = pg_cursor.fetchone()
        if result and result[0] is not None:
            pg_cursor.execute(
                f'SELECT COALESCE(MAX("{col_name}"), 0) FROM "{table_name}"'
            )
            max_id = pg_cursor.fetchone()[0] or 0
            pg_cursor.execute(
                "SELECT setval(pg_get_serial_sequence(%s, %s), %s, %s)",
                (table_name, col_name, max_id, bool(max_id))
            )

    for table in tables:
        print(f'---\nProcessing table: {table}')
        sqlite_rows = sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchall()
        if not sqlite_rows:
            print(f'ERROR: SQLite table not found: {table}')
            sqlite_counts[table] = None
            continue

        cols = sqlite_conn.execute(f'PRAGMA table_info({table})').fetchall()
        if not cols:
            print(f'ERROR: Could not inspect SQLite table: {table}')
            sqlite_counts[table] = None
            continue

        sqlite_count = sqlite_conn.execute(f'SELECT COUNT(*) AS count FROM {table}').fetchone()['count']
        sqlite_counts[table] = sqlite_count
        print(f'SQLite before count: {sqlite_count}')

        if not table_exists(pg_cur, table):
            print(f'Postgres table missing, creating: {table}')
            create_table(pg_cur, table, cols)
            pg_conn.commit()
        else:
            print(f'Postgres table exists: {table}')
            existing_cols = pg_columns(pg_cur, table)
            add_missing_columns(pg_cur, table, cols, existing_cols)
            pg_conn.commit()

        pg_cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        pg_before = pg_cur.fetchone()[0]
        pg_counts_before[table] = pg_before
        print(f'Postgres before count: {pg_before}')

        col_names = [col['name'] for col in cols]
        col_list = ', '.join(f'"{name}"' for name in col_names)
        insert_sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({", ".join(["%s"]*len(col_names))}) ON CONFLICT DO NOTHING'

        rows = sqlite_conn.execute(f'SELECT {col_list} FROM {table}').fetchall()
        if rows:
            psycopg2.extras.execute_batch(pg_cur, insert_sql, rows, page_size=100)
            pg_conn.commit()
            print(f'Inserted up to {len(rows)} rows into {table} (ON CONFLICT DO NOTHING)')
        else:
            print(f'No rows to migrate for {table}')

        pg_cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        pg_after = pg_cur.fetchone()[0]
        pg_counts_after[table] = pg_after
        print(f'Postgres after count: {pg_after}')

        if any(col['name'] == 'id' for col in cols):
            try:
                set_sequence(pg_cur, table, 'id')
                pg_conn.commit()
            except Exception:
                pass

    success = True
    for table in tables:
        sc = sqlite_counts.get(table)
        pc = pg_counts_after.get(table)
        if sc is None or pc is None or pc != sc:
            success = False

    print('---')
    print('---Final Summary---')
    for table in tables:
        print(f'{table}: sqlite={sqlite_counts.get(table)} postgres_before={pg_counts_before.get(table)} postgres_after={pg_counts_after.get(table)}')
    if success:
        print('Migration Successful')
    else:
        print('Migration Failed: counts did not match SQLite for all tables.')

finally:
    try:
        sqlite_conn.close()
    except Exception:
        pass
    try:
        pg_cur.close()
        pg_conn.close()
    except Exception:
        pass
