"""
Chunks of a job's source ID range — the unit of work that lets several userbot
accounts copy one job at the same time.

A job is sharded only when at least two accounts can reach both of its channels
(see CopyEngine._plan_shards). Until then no rows exist for it and the job runs
as a single ordered pass, exactly as it did before multi-account copying.

Claiming is atomic: the UPDATE ... WHERE status='pending' is what decides the
winner, so two accounts can never take the same chunk and never copy the same
message twice.
"""
from __future__ import annotations

from typing import Optional

from app import db
from app.models import Job, JobChunk

# Predicate on a joined `jobs j`: only jobs this account may work on — one it
# wasn't excluded from, whose channels it isn't known to lack access to, and that
# either has no allow-list or names this account in it. Mirrors job_repo's claim
# rules, and expects (str(userbot_id), userbot_id, str(userbot_id)) as params.
_CLAIMABLE_JOB = """
    j.status = 'running'
    AND (j.excluded_userbot_ids IS NULL OR j.excluded_userbot_ids = ''
         OR ',' || j.excluded_userbot_ids || ',' NOT LIKE '%,' || ? || ',%')
    AND NOT EXISTS (SELECT 1 FROM channel_access ca
                     WHERE ca.userbot_id = ? AND ca.has_access = 0
                       AND ((ca.channel_kind = 'source' AND ca.channel_id = j.source_id)
                         OR (ca.channel_kind = 'destination'
                             AND ca.channel_id = j.destination_id)))
    AND (j.allowed_userbot_ids IS NULL OR j.allowed_userbot_ids = ''
         OR ',' || j.allowed_userbot_ids || ',' LIKE '%,' || ? || ',%')
"""


def plan(job_id: int, ranges: list[tuple[int, int]]) -> int:
    """Write the chunk plan for a job. Ignores ranges already planned."""
    conn = db.get_connection()
    conn.executemany(
        "INSERT OR IGNORE INTO job_chunks (job_id, id_from, id_to) VALUES (?,?,?)",
        [(job_id, lo, hi) for lo, hi in ranges],
    )
    conn.commit()
    return len(ranges)


def count_for_job(job_id: int) -> int:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM job_chunks WHERE job_id = ?", (job_id,)
    ).fetchone()
    return row["cnt"] if row else 0


def count_unfinished(job_id: int) -> int:
    """Chunks still pending or in someone's hands."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM job_chunks WHERE job_id = ? AND status != 'done'",
        (job_id,),
    ).fetchone()
    return row["cnt"] if row else 0


def progress(job_id: int) -> tuple[int, int]:
    """(done, total) chunks — for progress display."""
    conn = db.get_connection()
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  COUNT(CASE WHEN status = 'done' THEN 1 END) AS done
             FROM job_chunks WHERE job_id = ?""",
        (job_id,),
    ).fetchone()
    if not row:
        return 0, 0
    return row["done"] or 0, row["total"] or 0


def active_userbot_ids(job_id: int) -> list[int]:
    """Accounts holding a chunk of this job right now — who is actually copying it."""
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT DISTINCT assigned_userbot_id FROM job_chunks
           WHERE job_id = ? AND status = 'running' AND assigned_userbot_id IS NOT NULL""",
        (job_id,),
    ).fetchall()
    return [r["assigned_userbot_id"] for r in rows]


def claim_next(job_id: int, userbot_id: int) -> Optional[JobChunk]:
    """Atomically take the lowest pending chunk of one job. None if there is none."""
    return _claim(
        "SELECT id FROM job_chunks WHERE job_id = ? AND status = 'pending' "
        "ORDER BY id_from ASC LIMIT 1",
        (job_id,),
        userbot_id,
    )


def claim_any(userbot_id: int) -> Optional[tuple[Job, JobChunk]]:
    """
    Atomically take a pending chunk of any running sharded job this account may
    work on — how a free account joins a job another account is already leading.

    Oldest job first, so the queue still drains in submit order instead of every
    account piling onto whichever job happens to be newest.
    """
    conn = db.get_connection()
    row = conn.execute(
        f"""SELECT job_chunks.id AS id FROM job_chunks
             JOIN jobs j ON j.id = job_chunks.job_id
            WHERE job_chunks.status = 'pending'
              AND {_CLAIMABLE_JOB}
            ORDER BY COALESCE(j.submitted_at, j.created_at) ASC,
                     job_chunks.job_id ASC, job_chunks.id_from ASC
            LIMIT 1""",  # nosec B608 — fixed fragment with bound params
        (str(userbot_id), userbot_id, str(userbot_id)),
    ).fetchone()
    if row is None:
        return None

    chunk = _take(row["id"], userbot_id)
    if chunk is None:
        return None  # another account won the race

    from app.repositories import job_repo
    job = job_repo.get_by_id(chunk.job_id)
    if job is None:
        release(chunk.id, userbot_id)
        return None
    return job, chunk


def _claim(select_sql: str, params: tuple, userbot_id: int) -> Optional[JobChunk]:
    conn = db.get_connection()
    row = conn.execute(select_sql, params).fetchone()
    if row is None:
        return None
    return _take(row["id"], userbot_id)


def _take(chunk_id: int, userbot_id: int) -> Optional[JobChunk]:
    """The atomic half of a claim: None means another account got there first."""
    conn = db.get_connection()
    cur = conn.execute(
        """UPDATE job_chunks SET
             status = 'running',
             assigned_userbot_id = ?,
             updated_at = datetime('now')
           WHERE id = ? AND status = 'pending'""",
        (userbot_id, chunk_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_by_id(chunk_id)


def get_by_id(chunk_id: int) -> Optional[JobChunk]:
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM job_chunks WHERE id = ?", (chunk_id,)).fetchone()
    return JobChunk.from_row(row) if row else None


def checkpoint(chunk_id: int, last_processed_id: int) -> None:
    """
    Record how far this chunk got.

    Doubles as the chunk's heartbeat: reclaim_stale reads updated_at to tell a
    slow account from one that died mid-chunk.
    """
    conn = db.get_connection()
    conn.execute(
        """UPDATE job_chunks SET
             last_processed_id = ?,
             updated_at = datetime('now')
           WHERE id = ?""",
        (last_processed_id, chunk_id),
    )
    conn.commit()


def mark_done(chunk_id: int, owner_id: Optional[int] = None) -> None:
    """Finish a chunk. Pass owner_id to make it a no-op if the chunk moved on."""
    conn = db.get_connection()
    if owner_id is None:
        conn.execute(
            "UPDATE job_chunks SET status='done', updated_at=datetime('now') WHERE id=?",
            (chunk_id,),
        )
    else:
        conn.execute(
            """UPDATE job_chunks SET status='done', updated_at=datetime('now')
               WHERE id=? AND assigned_userbot_id=?""",
            (chunk_id, owner_id),
        )
    conn.commit()


def release(chunk_id: int, owner_id: Optional[int] = None) -> None:
    """
    Hand a chunk back to the queue, keeping its checkpoint so whoever takes it
    next resumes instead of restarting.
    """
    conn = db.get_connection()
    if owner_id is None:
        conn.execute(
            """UPDATE job_chunks SET status='pending', assigned_userbot_id=NULL,
                 updated_at=datetime('now')
               WHERE id=? AND status='running'""",
            (chunk_id,),
        )
    else:
        conn.execute(
            """UPDATE job_chunks SET status='pending', assigned_userbot_id=NULL,
                 updated_at=datetime('now')
               WHERE id=? AND status='running' AND assigned_userbot_id=?""",
            (chunk_id, owner_id),
        )
    conn.commit()


def release_for_userbot(userbot_id: int) -> int:
    """Hand back every chunk this account holds — used when its runner stops."""
    conn = db.get_connection()
    cur = conn.execute(
        """UPDATE job_chunks SET status='pending', assigned_userbot_id=NULL,
             updated_at=datetime('now')
           WHERE status='running' AND assigned_userbot_id=?""",
        (userbot_id,),
    )
    conn.commit()
    return cur.rowcount


def release_all_running() -> int:
    """Startup recovery: nobody holds a chunk across a restart."""
    conn = db.get_connection()
    cur = conn.execute(
        """UPDATE job_chunks SET status='pending', assigned_userbot_id=NULL,
             updated_at=datetime('now')
           WHERE status='running'"""
    )
    conn.commit()
    return cur.rowcount


def reclaim_stale(job_id: int, older_than_s: int) -> int:
    """
    Put back chunks whose owner stopped reporting progress.

    The window has to be generous: a chunk heartbeats on every message it writes,
    so silence only means a very large single download — or a dead account. Taking
    a chunk back too early would let two accounts copy the same messages, which
    the copied_messages dedup cannot undo once both have sent.
    """
    conn = db.get_connection()
    cur = conn.execute(
        f"""UPDATE job_chunks SET status='pending', assigned_userbot_id=NULL,
              updated_at=datetime('now')
            WHERE job_id = ? AND status='running'
              AND updated_at <= datetime('now', '-{int(older_than_s)} seconds')""",  # nosec B608 — int-cast literal
        (job_id,),
    )
    conn.commit()
    return cur.rowcount


def delete_for_job(job_id: int) -> None:
    conn = db.get_connection()
    conn.execute("DELETE FROM job_chunks WHERE job_id = ?", (job_id,))
    conn.commit()
