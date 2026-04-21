"""Dispatch store — SQLite helpers for SmartChargeTesla."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .schema import init_db


class DispatchStore:
    def __init__(self, db_path: str = "data/smartcharge.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def upsert_dispatch(self, start: str, end: str, delta_kwh: float = None,
                        type: str = None, source: str = None, location: str = None):
        """Insert or update a dispatch window. (start, end) is the unique key."""
        fetched_at = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO dispatches (start, end, delta_kwh, type, source, location, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(start, end) DO UPDATE SET
                delta_kwh=excluded.delta_kwh,
                type=excluded.type,
                source=excluded.source,
                location=excluded.location,
                fetched_at=excluded.fetched_at
        """, (start, end, delta_kwh, type, source, location, fetched_at))
        self.conn.commit()

    def delete_dispatch(self, start: str, end: str):
        self.conn.execute("DELETE FROM dispatches WHERE start=? AND end=?", (start, end))
        self.conn.commit()

    def set_dispatch_gcal_event_id(self, start: str, event_id: str,
                                   sequence: int = 0, end: str = None):
        """Record the calendar event UID and SEQUENCE for a dispatch window."""
        if end:
            self.conn.execute(
                "UPDATE dispatches SET gcal_event_id=?, gcal_sequence=? "
                "WHERE start=? AND end=?",
                (event_id, sequence, start, end)
            )
        else:
            self.conn.execute(
                "UPDATE dispatches SET gcal_event_id=?, gcal_sequence=? WHERE start=?",
                (event_id, sequence, start)
            )
        self.conn.commit()

    def get_dispatches_needing_calendar_event(self) -> list[dict]:
        """Planned windows (type IS NOT NULL) that don't yet have a calendar event."""
        now = datetime.now(timezone.utc).isoformat()
        rows = self.conn.execute("""
            SELECT start, end, delta_kwh, type, source, location
            FROM dispatches
            WHERE gcal_event_id IS NULL AND type IS NOT NULL AND end > ?
            ORDER BY start
        """, (now,)).fetchall()
        return [dict(r) for r in rows]

    def get_dispatches(self, from_dt: str = None, to_dt: str = None) -> list[dict]:
        """Return dispatch windows, optionally filtered by UTC datetime range."""
        clauses, params = [], []
        if from_dt:
            clauses.append("start >= ?"); params.append(from_dt)
        if to_dt:
            clauses.append("start <= ?"); params.append(to_dt)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            f"SELECT start, end, delta_kwh, type, source, location, "
            f"gcal_event_id, gcal_sequence, fetched_at "
            f"FROM dispatches {where} ORDER BY start",
            params
        ).fetchall()
        return [dict(r) for r in rows]

    def get_planned_dispatches(self) -> list[dict]:
        """Return all planned (type IS NOT NULL) dispatch windows."""
        rows = self.conn.execute(
            "SELECT start, end, delta_kwh, type, source, location, "
            "gcal_event_id, gcal_sequence "
            "FROM dispatches WHERE type IS NOT NULL ORDER BY start"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
