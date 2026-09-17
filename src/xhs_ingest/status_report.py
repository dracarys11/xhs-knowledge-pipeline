"""Snapshot helper to dump StateStore status to JSON for verification."""

import json
import sqlite3
import sys
from pathlib import Path

from .state import DEFAULT_STATE_DB


def generate_status_report(db_path: Path) -> dict:
    if not db_path.exists():
        return {"error": f"Database file not found: {db_path}"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT count(*) as total, count(DISTINCT note_id) as distinct_notes FROM notes")
    totals = c.fetchone()
    c.execute("SELECT status, count(*) as cnt FROM notes GROUP BY status ORDER BY status")
    counts = {row["status"]: row["cnt"] for row in c.fetchall()}
    c.execute("SELECT note_id, status, attempt_count, last_error_status, updated_at FROM notes ORDER BY note_id")
    notes = [dict(row) for row in c.fetchall()]
    return {
        "tracked_count": totals["total"],
        "distinct_note_id_count": totals["distinct_notes"],
        "status_counts": counts,
        "notes": notes,
    }


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_STATE_DB
    report = generate_status_report(db_path)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
