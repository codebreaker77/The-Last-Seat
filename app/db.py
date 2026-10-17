import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://seat:seat@localhost:5432/seat')
pool = ConnectionPool(DATABASE_URL, min_size=2, max_size=20, open=False,
                      kwargs={'row_factory': dict_row, 'options': '-c statement_timeout=15000 -c lock_timeout=10000'})


def initialize():
    pool.open(wait=True)
    with pool.connection() as conn:
        # Every API process may start simultaneously. Serialize schema creation/seed.
        conn.execute('SELECT pg_advisory_xact_lock(741902)')
        conn.execute(Path(__file__).with_name('schema.sql').read_text())
        event_id = '00000000-0000-0000-0000-000000000001'
        conn.execute("INSERT INTO events(id,name,capacity,hold_seconds,opens_at) VALUES(%s,'The Last Seat',500,120,clock_timestamp()) ON CONFLICT DO NOTHING", (event_id,))
        conn.execute('INSERT INTO seats SELECT %s,generate_series(1,500) ON CONFLICT DO NOTHING', (event_id,))
