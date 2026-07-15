"""Repository for duplicate scan and bulk-delete operations."""
from __future__ import annotations

import logging
from typing import Any

from app.db import get_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Duplicate scans
# ---------------------------------------------------------------------------

def create_scan(channel_ref: str, channel_title: str, dest_id: int | None = None) -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO duplicate_scans(channel_ref, channel_title, dest_id) VALUES (?,?,?)",
        (channel_ref, channel_title, dest_id),
    )
    conn.commit()
    return cur.lastrowid


def start_scan(scan_id: int) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE duplicate_scans SET status='running' WHERE id=?", (scan_id,)
    )
    conn.commit()


def update_progress(scan_id: int, scanned: int, total: int) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE duplicate_scans SET messages_scanned=?, total_messages=? WHERE id=?",
        (scanned, total, scan_id),
    )
    conn.commit()


def finish_scan(scan_id: int, groups: int, wasted_count: int, report_url: str | None = None) -> None:
    conn = get_connection()
    # Store the highest message_id seen in this scan, for incremental future scans
    max_msg_row = conn.execute(
        "SELECT MAX(message_id) as max_id FROM duplicate_scan_items WHERE scan_id=?",
        (scan_id,),
    ).fetchone()
    max_msg_id = (max_msg_row["max_id"] or 0) if max_msg_row else 0

    conn.execute(
        """UPDATE duplicate_scans
           SET status='done', duplicate_groups=?, wasted_count=?,
               report_url=?, completed_at=datetime('now'),
               last_scanned_message_id=?
           WHERE id=?""",
        (groups, wasted_count, report_url, max_msg_id, scan_id),
    )
    conn.commit()


def fail_scan(scan_id: int, error_msg: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE duplicate_scans SET status='failed', error_msg=? WHERE id=?",
        (error_msg, scan_id),
    )
    conn.commit()


def cancel_scan(scan_id: int) -> None:
    """Mark a pending/running scan as failed (user-initiated stop)."""
    conn = get_connection()
    conn.execute(
        "UPDATE duplicate_scans SET status='failed', error_msg='בוטל על ידי המשתמש' WHERE id=? AND status IN ('pending','running')",
        (scan_id,),
    )
    conn.commit()


def delete_scan(scan_id: int) -> int:
    """Delete a single scan record (and its items/delete-jobs). Returns count deleted."""
    conn = get_connection()
    conn.execute(
        "DELETE FROM delete_scan_jobs WHERE scan_id=?",
        (scan_id,),
    )
    conn.execute(
        "DELETE FROM duplicate_scan_items WHERE scan_id=?",
        (scan_id,),
    )
    cur = conn.execute(
        "DELETE FROM duplicate_scans WHERE id=?",
        (scan_id,),
    )
    conn.commit()
    return cur.rowcount


def get_scans_for_channel(channel_ref: str) -> list[dict[str, Any]]:
    """Return all scans for a channel, ordered newest to oldest."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM duplicate_scans
           WHERE channel_ref=?
           ORDER BY created_at DESC""",
        (channel_ref,),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_item(
    scan_id: int,
    message_id: int,
    media_id: int,
    media_type: str,
    file_size: int | None,
    mime_type: str | None,
    msg_date: str,
) -> None:
    conn = get_connection()
    conn.execute(
        """INSERT INTO duplicate_scan_items
           (scan_id, message_id, media_id, media_type, file_size, mime_type, msg_date)
           VALUES (?,?,?,?,?,?,?)""",
        (scan_id, message_id, media_id, media_type, file_size, mime_type, msg_date),
    )
    # Caller commits in batches for performance


def get_duplicate_groups(scan_id: int) -> list[dict[str, Any]]:
    """Return groups where the same media_id appears more than once, sorted by count desc."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT media_id, media_type, mime_type,
                  MAX(file_size) AS file_size,
                  COUNT(*) AS total_count,
                  MIN(message_id) AS oldest_msg_id
           FROM duplicate_scan_items
           WHERE scan_id=?
           GROUP BY media_id
           HAVING COUNT(*) > 1
           ORDER BY COUNT(*) DESC""",
        (scan_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_items_for_media(scan_id: int, media_id: int) -> list[dict[str, Any]]:
    """Return all scan items for a specific media_id, ordered by date asc."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT message_id, msg_date
           FROM duplicate_scan_items
           WHERE scan_id=? AND media_id=?
           ORDER BY msg_date ASC""",
        (scan_id, media_id),
    ).fetchall()
    return [dict(r) for r in rows]


def reset_running_scans_to_pending() -> int:
    """Reset any scans stuck in 'running' back to 'pending' (used on startup and after network drops)."""
    conn = get_connection()
    cur = conn.execute(
        "UPDATE duplicate_scans SET status='pending' WHERE status='running'"
    )
    conn.commit()
    return cur.rowcount


def get_pending_scan() -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute(
        """SELECT id AS scan_id, channel_ref, channel_title, dest_id
           FROM duplicate_scans
           WHERE status='pending'
           ORDER BY created_at ASC
           LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def get_active_scan() -> dict[str, Any] | None:
    """Return the currently running or next pending scan, to show on the main menu."""
    conn = get_connection()
    row = conn.execute(
        """SELECT *
           FROM duplicate_scans
           WHERE status IN ('pending', 'running')
           ORDER BY status DESC, created_at ASC
           LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def get_scan_by_id(scan_id: int) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM duplicate_scans WHERE id=?", (scan_id,)
    ).fetchone()
    return dict(row) if row else None


def get_latest_scan_for_channel(channel_ref: str) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute(
        """SELECT * FROM duplicate_scans
           WHERE channel_ref=?
           ORDER BY created_at DESC
           LIMIT 1""",
        (channel_ref,),
    ).fetchone()
    return dict(row) if row else None


def get_last_scanned_message_id(channel_ref: str) -> int:
    """
    Return the highest message_id that was successfully scanned in any previous
    completed scan for this channel. Returns 0 if no prior scan exists.
    Used to enable incremental (delta) scans.
    """
    conn = get_connection()
    row = conn.execute(
        """SELECT MAX(last_scanned_message_id) as max_id
           FROM duplicate_scans
           WHERE channel_ref=? AND status='done'""",
        (channel_ref,),
    ).fetchone()
    return (row["max_id"] or 0) if row else 0


def get_all_known_media_ids_for_channel(channel_ref: str) -> set[int]:
    """
    Return all media_ids ever recorded for this channel across all scans.
    Used by incremental scan to detect cross-scan duplicates.
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT DISTINCT dsi.media_id
           FROM duplicate_scan_items dsi
           JOIN duplicate_scans ds ON ds.id = dsi.scan_id
           WHERE ds.channel_ref=? AND ds.status='done'""",
        (channel_ref,),
    ).fetchall()
    return {r["media_id"] for r in rows}


def get_all_scans(limit: int = 20) -> list[dict[str, Any]]:
    """Return recent scans for the scan list screen."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM duplicate_scans
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Delete scan jobs
# ---------------------------------------------------------------------------

def create_delete_job(scan_id: int) -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO delete_scan_jobs(scan_id) VALUES (?)", (scan_id,)
    )
    conn.commit()
    return cur.lastrowid


def get_pending_delete_job() -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute(
        """SELECT dsj.id, dsj.scan_id,
                  ds.channel_ref, ds.channel_title
           FROM delete_scan_jobs dsj
           JOIN duplicate_scans ds ON ds.id = dsj.scan_id
           WHERE dsj.status='pending'
           ORDER BY dsj.created_at ASC
           LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def get_active_delete_job() -> dict[str, Any] | None:
    """Return the currently running or next pending delete job, to show on the main menu."""
    conn = get_connection()
    row = conn.execute(
        """SELECT dsj.*,
                  ds.channel_ref, ds.channel_title
           FROM delete_scan_jobs dsj
           JOIN duplicate_scans ds ON ds.id = dsj.scan_id
           WHERE dsj.status IN ('pending', 'running')
           ORDER BY dsj.status DESC, dsj.created_at ASC
           LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def start_delete_job(job_id: int) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE delete_scan_jobs SET status='running' WHERE id=?", (job_id,)
    )
    conn.commit()


def finish_delete_job(job_id: int, deleted_count: int) -> None:
    conn = get_connection()
    conn.execute(
        """UPDATE delete_scan_jobs
           SET status='done', deleted_count=?, completed_at=datetime('now')
           WHERE id=?""",
        (deleted_count, job_id),
    )
    conn.commit()


def fail_delete_job(job_id: int, error_msg: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE delete_scan_jobs SET status='failed', error_msg=? WHERE id=?",
        (error_msg, job_id),
    )
    conn.commit()


def get_latest_delete_job(scan_id: int) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute(
        """SELECT * FROM delete_scan_jobs
           WHERE scan_id=?
           ORDER BY created_at DESC
           LIMIT 1""",
        (scan_id,),
    ).fetchone()
    return dict(row) if row else None
