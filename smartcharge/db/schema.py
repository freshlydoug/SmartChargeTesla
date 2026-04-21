"""SQLite schema for SmartChargeTesla dispatch tracking."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start TEXT NOT NULL,              -- ISO datetime (UTC) of dispatch window start
    end TEXT NOT NULL,                -- ISO datetime (UTC) of dispatch window end
    delta_kwh REAL,                   -- Energy imported during window (negative = import)
    type TEXT,                        -- 'SMART_FLEX' or NULL for completed dispatches
    source TEXT,                      -- Meta source field from Kraken ('unknown', 'grid', etc.)
    location TEXT,                    -- 'AT_HOME' etc.
    gcal_event_id TEXT,               -- Calendar event UID once sent
    gcal_sequence INTEGER NOT NULL DEFAULT 0,  -- iCalendar SEQUENCE for updates
    fetched_at TEXT NOT NULL,

    UNIQUE(start, end)
);

CREATE INDEX IF NOT EXISTS idx_dispatches_start ON dispatches(start);
"""

_MIGRATIONS = [
    "ALTER TABLE dispatches ADD COLUMN gcal_event_id TEXT",
    "ALTER TABLE dispatches ADD COLUMN gcal_sequence INTEGER NOT NULL DEFAULT 0",
    # Widen unique key from (start) to (start, end) so SMART_FLEX and completed
    # records with the same start time can coexist.
    """
    CREATE TABLE IF NOT EXISTS dispatches_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start TEXT NOT NULL,
        end TEXT NOT NULL,
        delta_kwh REAL,
        type TEXT,
        source TEXT,
        location TEXT,
        gcal_event_id TEXT,
        gcal_sequence INTEGER NOT NULL DEFAULT 0,
        fetched_at TEXT NOT NULL,
        UNIQUE(start, end)
    );
    INSERT OR IGNORE INTO dispatches_new
        SELECT id, start, end, delta_kwh, type, source, location,
               gcal_event_id, COALESCE(gcal_sequence, 0), fetched_at
        FROM dispatches;
    DROP TABLE dispatches;
    ALTER TABLE dispatches_new RENAME TO dispatches;
    CREATE INDEX IF NOT EXISTS idx_dispatches_start ON dispatches(start);
    """,
]


def init_db(conn):
    conn.executescript(SCHEMA)
    for sql in _MIGRATIONS:
        try:
            if "CREATE TABLE IF NOT EXISTS dispatches_new" in sql:
                idx = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='dispatches'"
                ).fetchone()
                if idx and "UNIQUE(start)" in idx[0] and "UNIQUE(start, end)" not in idx[0]:
                    conn.executescript(sql)
            else:
                conn.execute(sql)
                conn.commit()
        except Exception:
            pass  # already applied
