"""
Dated history of a job's errors.

jobs.error_message only ever holds the latest error: every new one overwrites it,
and a completed job clears it. This table keeps them all with a timestamp and the
account that hit them, which is what the errors screen renders.

Writes go through job_repo.update_status, so nothing else has to remember to log.
"""
from __future__ import annotations

from typing import Optional

from app import db

# Errors of a stuck job repeat every retry. Keeping the whole stream would bury
# the interesting ones, so a repeat of the newest entry only bumps its timestamp.
_MAX_LEN = 200
# Longest error text kept per entry. Also the length the dedup compares at.
_MAX_ERROR_LEN = 500


def add(job_id: int, error: str, userbot_id: Optional[int] = None) -> None:
    # Truncate before comparing, not only before inserting: comparing the full
    # incoming text against an already-truncated stored one never matches, so an
    # error longer than the limit inserted a fresh row on every single retry and
    # pushed the interesting entries out of the history the dedup exists to protect.
    error = (error or "")[:_MAX_ERROR_LEN]
    conn = db.get_connection()
    latest = conn.execute(
        "SELECT id, error, userbot_id FROM job_errors WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()

    if latest and latest["error"] == error and latest["userbot_id"] == userbot_id:
        conn.execute(
            "UPDATE job_errors SET created_at = datetime('now') WHERE id = ?",
            (latest["id"],),
        )
    else:
        conn.execute(
            "INSERT INTO job_errors (job_id, userbot_id, error) VALUES (?,?,?)",
            (job_id, userbot_id, error),
        )
        conn.execute(
            """DELETE FROM job_errors
                WHERE job_id = ?
                  AND id NOT IN (SELECT id FROM job_errors WHERE job_id = ?
                                 ORDER BY id DESC LIMIT ?)""",
            (job_id, job_id, _MAX_LEN),
        )
    conn.commit()


def count(job_id: int) -> int:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM job_errors WHERE job_id = ?", (job_id,)
    ).fetchone()
    return row["cnt"] if row else 0


def page(job_id: int, offset: int = 0, limit: int = 10) -> list[dict]:
    """Newest first. Each entry: id, userbot_id, error, created_at."""
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT id, userbot_id, error, created_at FROM job_errors
            WHERE job_id = ? ORDER BY id DESC LIMIT ? OFFSET ?""",
        (job_id, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]

# No clear(): the history is pruned by add() and removed with the job in
# job_repo.delete. A restart deliberately keeps it — see restart_failed_job.
