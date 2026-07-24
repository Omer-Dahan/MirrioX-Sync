"""
Durable per-channel-pair sync watermark.

One row per (source_id, destination_id): the highest source message id already
synced to that destination, and when. Keyed on the channel pair rather than on a
job, so it survives job deletion — that is the whole point. A future re-sync of
the same pair can seed itself from here and skip the history it already covered,
instead of scanning everything again.

Only full-history ('all'), single-destination jobs feed this table. For those the
watermark means exactly "everything up to id X has been delivered to this
destination". An id_range/date_range job, or a random fan-out to several
destinations, does not carry that guarantee, so it deliberately leaves the
watermark alone (see record_from_job).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.db import get_connection

logger = logging.getLogger(__name__)


def bump(
    source_id: int,
    destination_id: int,
    synced_id: int,
    when: Optional[str] = None,
    job_name: Optional[str] = None,
) -> None:
    """
    Raise the pair's watermark to `synced_id` (never lowers it).

    `when` is the timestamp of that progress (defaults to now); it is only kept
    when it actually advances the row, so a stale re-record can't backdate it.
    """
    if not synced_id or synced_id <= 0:
        return
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO channel_sync_state
            (source_id, destination_id, last_synced_id, last_synced_at, last_job_name, updated_at)
        VALUES (?, ?, ?, COALESCE(?, datetime('now')), ?, datetime('now'))
        ON CONFLICT(source_id, destination_id) DO UPDATE SET
            last_synced_at = CASE
                WHEN excluded.last_synced_id > channel_sync_state.last_synced_id
                THEN COALESCE(excluded.last_synced_at, datetime('now'))
                ELSE channel_sync_state.last_synced_at END,
            last_job_name  = CASE
                WHEN excluded.last_synced_id > channel_sync_state.last_synced_id
                THEN COALESCE(excluded.last_job_name, channel_sync_state.last_job_name)
                ELSE channel_sync_state.last_job_name END,
            last_synced_id = MAX(channel_sync_state.last_synced_id, excluded.last_synced_id),
            updated_at     = datetime('now')
        """,
        (source_id, destination_id, synced_id, when, job_name),
    )
    conn.commit()


def record_from_job(job) -> None:
    """
    Capture a job's progress into the pair watermark, if it qualifies.

    No-op unless the job is a full-history ('all'), single-destination run — the
    only shape for which "processed up to id X" means the whole 1..X range reached
    one specific channel. The watermark stops just below the first message that
    failed to copy: everything at or below it was actually delivered, which is what
    makes it a safe resume point. A future re-sync seeds from here, so advancing it
    past a failed message would skip that message forever.

    Called both when a job finishes/starts listening and just before it is
    deleted, so a listening job removed to declutter the queue still leaves its
    place behind.
    """
    if getattr(job, "mode", None) != "all":
        return
    dests = job.destination_id_list()
    if len(dests) != 1:
        return

    from app.repositories import job_repo
    watermark = job_repo.get_safe_watermark_source_id(job.id)
    if not watermark:
        return
    bump(job.source_id, dests[0], watermark, job_name=getattr(job, "name", None))
    logger.info(
        "Sync watermark for source #%d → destination #%d set to #%d (job #%d)",
        job.source_id, dests[0], watermark, job.id,
    )


def get_watermark(source_id: int, destination_id: int) -> int:
    """Highest source id already synced for this pair, or 0 if none recorded."""
    conn = get_connection()
    row = conn.execute(
        "SELECT last_synced_id FROM channel_sync_state WHERE source_id = ? AND destination_id = ?",
        (source_id, destination_id),
    ).fetchone()
    return row["last_synced_id"] if row and row["last_synced_id"] else 0


def get(source_id: int, destination_id: int) -> Optional[dict[str, Any]]:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM channel_sync_state WHERE source_id = ? AND destination_id = ?",
        (source_id, destination_id),
    ).fetchone()
    return dict(row) if row else None


def get_all() -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM channel_sync_state ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]
