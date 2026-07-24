"""CRUD and lifecycle operations for the jobs table."""
from __future__ import annotations

from typing import Optional
from app import db
from app.models import DEFAULT_CONTENT_TYPES, Job


def create(
    name: str,
    source_id: int,
    destination_id: int,
    mode: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    id_from: Optional[int] = None,
    id_to: Optional[int] = None,
    single_message_id: Optional[int] = None,
    use_blocked_words: bool = True,
    group_media: bool = True,
    copy_text: bool = True,
    content_types: str = DEFAULT_CONTENT_TYPES,
    created_by: Optional[int] = None,
    continuous: bool = False,
    allowed_userbot_ids: Optional[str] = None,
    destination_ids: Optional[list[int]] = None,
) -> Job:
    ids = destination_ids or [destination_id]
    extra = ",".join(str(i) for i in ids) if len(ids) > 1 else None
    conn = db.get_connection()
    cur = conn.execute(
        """INSERT INTO jobs
           (name, source_id, destination_id, mode,
            date_from, date_to, id_from, id_to, single_message_id,
            use_blocked_words, group_media, copy_text, content_types, created_by,
            continuous, allowed_userbot_ids, destination_ids)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            name, source_id, ids[0], mode,
            date_from, date_to, id_from, id_to, single_message_id,
            1 if use_blocked_words else 0,
            1 if group_media else 0,
            1 if copy_text else 0,
            content_types,
            created_by,
            1 if continuous else 0,
            allowed_userbot_ids,
            extra,
        ),
    )
    conn.commit()
    return get_by_id(cur.lastrowid)  # type: ignore[arg-type]


def get_by_id(job_id: int) -> Optional[Job]:
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


_STATUS_PRIORITY = """CASE status
    WHEN 'running'       THEN 1
    WHEN 'pending'       THEN 2
    WHEN 'waiting_retry' THEN 3
    WHEN 'paused'        THEN 4
    WHEN 'draft'         THEN 5
    WHEN 'failed'        THEN 6
    WHEN 'cancelled'     THEN 7
    WHEN 'completed'     THEN 8
    ELSE 9
END"""


def get_all(
    status_filter: Optional[list[str]] = None,
    created_by: Optional[int] = None,
) -> list[Job]:
    conn = db.get_connection()
    conditions = []
    params: list = []
    if status_filter:
        placeholders = ",".join("?" * len(status_filter))
        conditions.append(f"status IN ({placeholders})")  # nosec B608
        params.extend(status_filter)
    if created_by is not None:
        # Show jobs owned by this user OR unassigned (created before per-user tracking)
        conditions.append("(created_by = ? OR created_by IS NULL)")
        params.append(created_by)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    # Continuous (background) jobs sort last — they are low priority by design.
    query = (  # nosec B608
        f"SELECT * FROM jobs {where} "
        f"ORDER BY COALESCE(continuous,0) ASC, {_STATUS_PRIORITY} ASC, id DESC"
    )
    rows = conn.execute(query, params).fetchall()
    return [Job.from_row(r) for r in rows]


def get_pending_job() -> Optional[Job]:
    """Return the next pending bulk job in submit order (FIFO by submitted_at, fallback to id)."""
    conn = db.get_connection()
    row = conn.execute(
        """SELECT * FROM jobs
           WHERE status = 'pending' AND COALESCE(continuous,0) = 0
           ORDER BY COALESCE(submitted_at, created_at) ASC, id ASC LIMIT 1"""
    ).fetchone()
    return Job.from_row(row) if row else None


def get_resumable_job() -> Optional[Job]:
    """Return a waiting_retry bulk job whose retry time has passed (submit order)."""
    conn = db.get_connection()
    row = conn.execute(
        """SELECT * FROM jobs
           WHERE status = 'waiting_retry' AND COALESCE(continuous,0) = 0
             AND (next_retry_at IS NULL OR next_retry_at <= datetime('now'))
           ORDER BY COALESCE(submitted_at, created_at) ASC, id ASC LIMIT 1"""
    ).fetchone()
    return Job.from_row(row) if row else None


# ── Multi-userbot claiming ─────────────────────────────────────────────────────

# A userbot may not claim a job it has already been excluded from (no channel access).
_NOT_EXCLUDED = (
    "(excluded_userbot_ids IS NULL OR excluded_userbot_ids = '' "
    "OR ',' || excluded_userbot_ids || ',' NOT LIKE '%,' || ? || ',%')"
)

# Nor a job whose source or destination this account is *known* to have no access
# to — channel_access turns what the exclusion list rediscovers per job into a
# per-channel fact every job benefits from, so no cycle is wasted claiming work
# that can only fail.
#
# Deliberately a negative test: a channel this account hasn't probed yet stays
# claimable. Requiring a positive result instead would stall the whole queue
# whenever the checks lag behind (or no account has probed a brand-new channel).
_NO_KNOWN_LACK_OF_ACCESS = (
    "NOT EXISTS (SELECT 1 FROM channel_access ca "
    "WHERE ca.userbot_id = ? AND ca.has_access = 0 "
    "AND ((ca.channel_kind = 'source' AND ca.channel_id = jobs.source_id) "
    "OR (ca.channel_kind = 'destination' AND (ca.channel_id = jobs.destination_id "
    "OR (jobs.destination_ids IS NOT NULL "
    "AND ',' || jobs.destination_ids || ',' LIKE '%,' || ca.channel_id || ',%')))))"
)

# A user-chosen allow-list: when set, only the accounts it names may claim the
# job. NULL/empty imposes no restriction, so jobs created before this feature —
# and any job left on "all accounts" — keep claiming exactly as before.
_ALLOWED = (
    "(allowed_userbot_ids IS NULL OR allowed_userbot_ids = '' "
    "OR ',' || allowed_userbot_ids || ',' LIKE '%,' || ? || ',%')"
)


def claim_next_job(userbot_id: int) -> Optional[Job]:
    """
    Atomically claim the next runnable copy job for this userbot.

    This covers plain bulk jobs *and* continuous jobs that still owe their
    backfill — a continuous job copies the history its mode selects before it
    starts listening, so its first phase is an ordinary bulk run. Continuous
    jobs sort last, keeping them the low-priority background work they are.

    Ready retries come first, then pending jobs in submit order. Jobs this
    userbot was excluded from, or whose channels it is known to have no access
    to, are never returned.
    """
    conn = db.get_connection()
    row = conn.execute(
        f"""SELECT id FROM jobs
            WHERE (COALESCE(continuous,0) = 0 OR COALESCE(backfill_done,0) = 0)
              AND assigned_userbot_id IS NULL
              AND (
                    status = 'pending'
                    OR (status = 'waiting_retry'
                        AND (next_retry_at IS NULL OR next_retry_at <= datetime('now')))
              )
              AND {_NOT_EXCLUDED}
              AND {_NO_KNOWN_LACK_OF_ACCESS}
              AND {_ALLOWED}
            ORDER BY COALESCE(continuous,0) ASC,
                     CASE status WHEN 'waiting_retry' THEN 1 ELSE 2 END,
                     COALESCE(submitted_at, created_at) ASC, id ASC
            LIMIT 1""",  # nosec B608 — fixed fragments with bound params
        (str(userbot_id), userbot_id, str(userbot_id)),
    ).fetchone()
    if row is None:
        return None

    cur = conn.execute(
        """UPDATE jobs SET
             status = 'running',
             assigned_userbot_id = ?,
             started_at = COALESCE(started_at, datetime('now')),
             last_updated_at = datetime('now')
           WHERE id = ?
             AND assigned_userbot_id IS NULL
             AND status IN ('pending','waiting_retry')""",
        (userbot_id, row["id"]),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None  # another userbot won the race
    return get_by_id(row["id"])


def claim_continuous_job(userbot_id: int) -> Optional[Job]:
    """
    Atomically claim a continuous job that is ready to listen.

    Only jobs whose backfill has finished qualify — until then the job belongs
    to the bulk queue, where it copies its history.
    """
    conn = db.get_connection()
    row = conn.execute(
        f"""SELECT id FROM jobs
            WHERE COALESCE(continuous,0) = 1
              AND COALESCE(backfill_done,0) = 1
              AND assigned_userbot_id IS NULL
              AND status IN ('pending','running')
              AND {_NOT_EXCLUDED}
              AND {_NO_KNOWN_LACK_OF_ACCESS}
              AND {_ALLOWED}
            ORDER BY COALESCE(submitted_at, created_at) ASC, id ASC
            LIMIT 1""",  # nosec B608 — fixed fragments with bound params
        (str(userbot_id), userbot_id, str(userbot_id)),
    ).fetchone()
    if row is None:
        return None

    cur = conn.execute(
        """UPDATE jobs SET
             status = 'running',
             assigned_userbot_id = ?,
             started_at = COALESCE(started_at, datetime('now')),
             last_updated_at = datetime('now')
           WHERE id = ? AND assigned_userbot_id IS NULL AND status IN ('pending','running')""",
        (userbot_id, row["id"]),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_by_id(row["id"])


def get_continuous_jobs_for(userbot_id: int) -> list[Job]:
    """Continuous jobs assigned to this userbot that are past backfill and listening."""
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE COALESCE(continuous,0) = 1
             AND COALESCE(backfill_done,0) = 1
             AND assigned_userbot_id = ?
             AND status = 'running'
           ORDER BY id ASC""",
        (userbot_id,),
    ).fetchall()
    return [Job.from_row(r) for r in rows]


def mark_backfill_done(job_id: int) -> bool:
    """
    History copy finished — the job now moves to its listening phase.

    Deliberately leaves the status at 'running' instead of 'completed': a
    continuous job never completes, it just changes phase.

    Returns True only for the caller that actually made the transition. On a
    sharded job several accounts can finish their last chunk at the same moment
    and all try to close it; the flag itself decides the winner, so the finishing
    work that follows happens once.
    """
    conn = db.get_connection()
    cur = conn.execute(
        """UPDATE jobs SET
             backfill_done = 1,
             assigned_userbot_id = NULL,
             error_message = NULL,
             last_updated_at = datetime('now')
           WHERE id = ? AND COALESCE(backfill_done, 0) = 0""",
        (job_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def release_job(job_id: int, status: str = "pending") -> None:
    """Drop the userbot assignment and put the job back in the queue."""
    conn = db.get_connection()
    conn.execute(
        """UPDATE jobs SET
             status = ?,
             assigned_userbot_id = NULL,
             last_updated_at = datetime('now')
           WHERE id = ?""",
        (status, job_id),
    )
    conn.commit()


def clear_assignment(job_id: int, owner_id: Optional[int] = None) -> None:
    """
    Clear only the assignment, leaving the status untouched.

    Pass owner_id to make it a no-op unless that userbot still owns the job —
    this stops a finishing runner from clearing an assignment that has since
    been handed to a different account.
    """
    conn = db.get_connection()
    if owner_id is None:
        conn.execute(
            "UPDATE jobs SET assigned_userbot_id = NULL, last_updated_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
    else:
        conn.execute(
            """UPDATE jobs SET assigned_userbot_id = NULL, last_updated_at = datetime('now')
               WHERE id = ? AND assigned_userbot_id = ?""",
            (job_id, owner_id),
        )
    conn.commit()


def release_continuous_jobs(userbot_id: int) -> int:
    """
    Unassign this account's continuous jobs so another account can take them over.

    Called when a runner stops for any reason (disabled, unauthorized, crash,
    shutdown). The status stays 'running' — claim_continuous_job accepts a
    running-but-unassigned job — so the listener simply migrates to another
    account. Without this the job would keep pointing at a dead runner and no
    one could ever claim it, while the UI still showed it as listening.

    Only listening jobs (backfill_done=1) are released. A continuous job still
    copying its history is an ordinary bulk run owned by the copy engine, and
    handing it to a second account would duplicate work.
    """
    conn = db.get_connection()
    cur = conn.execute(
        """UPDATE jobs SET
             assigned_userbot_id = NULL,
             last_updated_at = datetime('now')
           WHERE COALESCE(continuous,0) = 1
             AND COALESCE(backfill_done,0) = 1
             AND assigned_userbot_id = ?""",
        (userbot_id,),
    )
    conn.commit()
    return cur.rowcount


def clear_all_assignments() -> int:
    """Startup recovery: forget every assignment from the previous run."""
    conn = db.get_connection()
    cur = conn.execute(
        "UPDATE jobs SET assigned_userbot_id = NULL WHERE assigned_userbot_id IS NOT NULL"
    )
    conn.commit()
    return cur.rowcount


def exclude_userbot(job_id: int, userbot_id: int) -> set[int]:
    """
    Mark this userbot as unable to run this job (no channel access) so it is
    never handed the job again. Returns the full exclusion set afterwards.
    """
    job = get_by_id(job_id)
    if job is None:
        return set()
    excluded = job.excluded_ids()
    excluded.add(userbot_id)
    conn = db.get_connection()
    conn.execute(
        "UPDATE jobs SET excluded_userbot_ids = ?, last_updated_at = datetime('now') WHERE id = ?",
        (",".join(str(i) for i in sorted(excluded)), job_id),
    )
    conn.commit()
    return excluded


# Boolean columns that may be edited on a draft/paused job without touching its
# copy progress. Whitelisted so update_flags can build SQL safely.
_EDITABLE_FLAGS = frozenset({"use_blocked_words", "group_media", "copy_text", "continuous"})


def update_flags(job_id: int, **flags: bool) -> None:
    """Update one or more boolean settings on a job. Unknown keys are ignored."""
    cols = []
    params: list = []
    for key, value in flags.items():
        if key not in _EDITABLE_FLAGS:
            continue
        cols.append(f"{key} = ?")
        params.append(1 if value else 0)
    if not cols:
        return
    params.append(job_id)
    conn = db.get_connection()
    conn.execute(
        f"UPDATE jobs SET {', '.join(cols)}, last_updated_at = datetime('now') "  # nosec B608 — cols from whitelist
        "WHERE id = ?",
        params,
    )
    conn.commit()


def set_content_types(job_id: int, content_types: str) -> None:
    conn = db.get_connection()
    conn.execute(
        "UPDATE jobs SET content_types = ?, last_updated_at = datetime('now') WHERE id = ?",
        (content_types, job_id),
    )
    conn.commit()


def set_allowed_userbots(job_id: int, allowed_userbot_ids: Optional[str]) -> None:
    """Set (or clear, with None) the account allow-list of a job."""
    conn = db.get_connection()
    conn.execute(
        "UPDATE jobs SET allowed_userbot_ids = ?, last_updated_at = datetime('now') WHERE id = ?",
        (allowed_userbot_ids, job_id),
    )
    conn.commit()


def set_destinations(job_id: int, destination_ids: list[int]) -> None:
    """Replace the job's destination list. First id becomes the primary destination_id."""
    value = ",".join(str(i) for i in destination_ids) if len(destination_ids) > 1 else None
    conn = db.get_connection()
    conn.execute(
        "UPDATE jobs SET destination_id = ?, destination_ids = ?, "
        "last_updated_at = datetime('now') WHERE id = ?",
        (destination_ids[0], value, job_id),
    )
    conn.commit()


def reset_exclusions(job_id: int) -> None:
    """Clear the exclusion list (e.g. after a new userbot is added)."""
    conn = db.get_connection()
    conn.execute(
        "UPDATE jobs SET excluded_userbot_ids = NULL WHERE id = ?", (job_id,)
    )
    conn.commit()


def reset_all_exclusions() -> int:
    """A newly added userbot may have access where others didn't — give jobs another chance."""
    conn = db.get_connection()
    cur = conn.execute(
        "UPDATE jobs SET excluded_userbot_ids = NULL WHERE excluded_userbot_ids IS NOT NULL"
    )
    conn.commit()
    return cur.rowcount


def get_active_job() -> Optional[Job]:
    """Return any job that is currently in an active state.

    Bulk jobs win over continuous ones, so the main menu reports real work in
    progress and only falls back to a background listener when nothing else runs.
    """
    conn = db.get_connection()
    row = conn.execute(
        """SELECT * FROM jobs
           WHERE status IN ('pending','running','waiting_retry')
           ORDER BY
               COALESCE(continuous,0) ASC,
               CASE status WHEN 'running' THEN 1 WHEN 'waiting_retry' THEN 2 WHEN 'pending' THEN 3 END ASC,
               COALESCE(started_at, submitted_at, created_at) ASC
           LIMIT 1"""
    ).fetchone()
    return Job.from_row(row) if row else None


def update_status(
    job_id: int,
    status: str,
    error: Optional[str] = None,
    next_retry_at: Optional[str] = None,
    userbot_id: Optional[int] = None,
) -> None:
    conn = db.get_connection()
    # Record submit time the first time the job becomes pending
    if status == "pending":
        conn.execute(
            """UPDATE jobs SET
                 status = ?,
                 submitted_at = COALESCE(submitted_at, datetime('now')),
                 error_message = COALESCE(?, error_message),
                 next_retry_at = ?,
                 last_updated_at = datetime('now')
               WHERE id = ?""",
            (status, error, next_retry_at, job_id),
        )
    else:
        conn.execute(
            """UPDATE jobs SET
                 status = ?,
                 error_message = COALESCE(?, error_message),
                 next_retry_at = ?,
                 last_updated_at = datetime('now')
               WHERE id = ?""",
            (status, error, next_retry_at, job_id),
        )
    conn.commit()
    # Every error write funnels through here, so this is the one place that has to
    # append to the dated log the errors screen reads. Written after the status,
    # not before: a failed UPDATE would otherwise leave a history entry for a
    # state the job never actually entered.
    if error:
        from app.repositories import job_error_repo
        job_error_repo.add(job_id, error, userbot_id)


def mark_started(job_id: int) -> None:
    conn = db.get_connection()
    conn.execute(
        """UPDATE jobs SET
             status = 'running',
             started_at = COALESCE(started_at, datetime('now')),
             last_updated_at = datetime('now')
           WHERE id = ?""",
        (job_id,),
    )
    conn.commit()


def mark_completed(job_id: int) -> bool:
    """
    Close a job as completed. True only for the caller that made the transition.

    The 'running' guard is what keeps a sharded job's finishing work — the retry
    pass, the summary line, the Telegraph report — from running twice when two
    accounts close their last chunk at the same moment. It also refuses to
    overwrite a job the user paused or cancelled in the meantime.
    """
    # error_message is cleared here, not left to update_status: that one keeps the
    # previous error when passed None (COALESCE), so a job that failed, retried and
    # then succeeded went on showing "שגיאה אחרונה" next to a green completed badge.
    conn = db.get_connection()
    cur = conn.execute(
        """UPDATE jobs SET
             status = 'completed',
             completed_at = datetime('now'),
             error_message = NULL,
             last_updated_at = datetime('now')
           WHERE id = ? AND status = 'running'""",
        (job_id,),
    )
    conn.commit()
    return cur.rowcount > 0


# How far back to look when measuring a job's real throughput, and how much
# evidence is needed before that measurement is trusted over the settings-based
# estimate. Short enough to react to a job speeding up or slowing down, long
# enough that a single batch pause doesn't dominate it.
_THROUGHPUT_WINDOW_MIN = 30
_THROUGHPUT_MIN_SAMPLES = 30
_THROUGHPUT_MIN_SPAN_S = 120


def recent_throughput(job_id: int, window_minutes: int = _THROUGHPUT_WINDOW_MIN) -> Optional[float]:
    """
    Messages actually processed per second lately, or None without enough signal.

    Measured rather than modelled, so it already contains everything the settings
    can't predict: how many accounts are on the job, how many messages get skipped
    for free, albums sent as one call, FloodWait pauses and reconnects.

    The span runs to *now*, not to the newest row, so a job that has gone quiet
    reports a falling rate instead of the rate it had before it stalled.
    """
    conn = db.get_connection()
    row = conn.execute(
        """SELECT COUNT(*) AS n,
                  (julianday('now') - julianday(MIN(processed_at))) * 86400.0 AS span_s
             FROM copied_messages
            WHERE job_id = ? AND processed_at >= datetime('now', ?)""",
        (job_id, f"-{int(window_minutes)} minutes"),
    ).fetchone()

    if not row or not row["n"] or row["span_s"] is None:
        return None
    n, span_s = row["n"], row["span_s"]
    if n < _THROUGHPUT_MIN_SAMPLES or span_s < _THROUGHPUT_MIN_SPAN_S:
        return None
    return n / span_s


def add_progress(
    job_id: int,
    copied: int = 0,
    skipped: int = 0,
    failed: int = 0,
    last_processed_id: Optional[int] = None,
) -> None:
    """
    Add to a job's counters.

    Deliberately additive rather than absolute: a sharded job is copied by several
    accounts at once, and each only knows its own tally. Writing absolute totals
    would make every account overwrite the others' progress.

    last_processed_id is the job-wide checkpoint and only means anything for an
    unsharded, strictly ascending run — a sharded job keeps a checkpoint per chunk
    instead, so its callers leave this None.
    """
    conn = db.get_connection()
    if last_processed_id is None:
        conn.execute(
            """UPDATE jobs SET
                 copied_count = COALESCE(copied_count,0) + ?,
                 skipped_count = COALESCE(skipped_count,0) + ?,
                 failed_count = COALESCE(failed_count,0) + ?,
                 last_updated_at = datetime('now')
               WHERE id = ?""",
            (copied, skipped, failed, job_id),
        )
    else:
        conn.execute(
            """UPDATE jobs SET
                 copied_count = COALESCE(copied_count,0) + ?,
                 skipped_count = COALESCE(skipped_count,0) + ?,
                 failed_count = COALESCE(failed_count,0) + ?,
                 last_processed_id = ?,
                 last_updated_at = datetime('now')
               WHERE id = ?""",
            (copied, skipped, failed, last_processed_id, job_id),
        )
    conn.commit()


def increment_retry(job_id: int) -> int:
    """Increment retry counter and return the new count."""
    conn = db.get_connection()
    conn.execute(
        "UPDATE jobs SET retry_count = retry_count + 1 WHERE id = ?", (job_id,)
    )
    conn.commit()
    row = conn.execute(
        "SELECT retry_count FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return row["retry_count"] if row else 0


def reset_retry(job_id: int) -> None:
    """
    Clear the retry counter after a long clean stretch of copying.

    The counter is meant to measure *consecutive* trouble, but nothing ever
    reset it mid-run: five unrelated FloodWaits spread over hours of successful
    work added up to "max retries reached" and paused a perfectly healthy job.
    """
    conn = db.get_connection()
    conn.execute(
        "UPDATE jobs SET retry_count = 0 WHERE id = ? AND retry_count > 0", (job_id,)
    )
    conn.commit()


def pause_job(job_id: int) -> None:
    # Clearing the assignment matters for continuous jobs: without it the job
    # would keep its old owner and could never be re-claimed after a resume.
    conn = db.get_connection()
    conn.execute(
        """UPDATE jobs SET
             status='paused',
             assigned_userbot_id=NULL,
             last_updated_at=datetime('now')
           WHERE id=? AND status IN ('running','pending','waiting_retry')""",
        (job_id,),
    )
    conn.commit()


def resume_job(job_id: int) -> None:
    conn = db.get_connection()
    conn.execute(
        """UPDATE jobs SET
             status='pending',
             assigned_userbot_id=NULL,
             last_updated_at=datetime('now')
           WHERE id=? AND status='paused'""",
        (job_id,),
    )
    conn.commit()


def restart_failed_job(job_id: int) -> bool:
    """
    Put a failed job back in the queue, continuing from where it stopped.

    last_processed_id and the job_chunks rows are left untouched on purpose —
    they are the checkpoint the copy engine resumes from. The retry bookkeeping
    is reset, otherwise a job that failed on the retry cap would fail again on
    its first transient error. Returns False if the job was not failed.

    report_url is cleared with it: it describes the run that failed, and leaving
    it up meant the job screen offered a stale report as though it belonged to the
    run now in progress. The new run writes its own when it finishes. The
    job_errors history is deliberately kept — that is the record of why this job
    needed restarting.
    """
    conn = db.get_connection()
    cur = conn.execute(
        """UPDATE jobs SET
             status='pending',
             assigned_userbot_id=NULL,
             submitted_at=datetime('now'),
             retry_count=0,
             next_retry_at=NULL,
             error_message=NULL,
             completed_at=NULL,
             report_url=NULL,
             last_updated_at=datetime('now')
           WHERE id=? AND status='failed'""",
        (job_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def is_paused(job_id: int) -> bool:
    conn = db.get_connection()
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    return bool(row and row["status"] == "paused")


def should_stop(job_id: int) -> bool:
    """
    True if a running job must stop now — the user paused or cancelled it, or
    the row is gone (deleted).

    The copy engine polls this between sends. Checking only 'paused' (as the
    engine used to) made 'cancel' invisible to a running job: it kept sending
    until the source was exhausted, and then overwrote 'cancelled' with
    'completed'.
    """
    conn = db.get_connection()
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return True
    return row["status"] in ("paused", "cancelled")


def is_cancelled(job_id: int) -> bool:
    conn = db.get_connection()
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    return bool(row and row["status"] == "cancelled")


def delete(job_id: int) -> bool:
    # Persist the pair's sync watermark before the job is gone. copied_messages
    # survives the delete, but nothing else remembers "this pair is synced up to
    # here" once the job row is removed — so a listening job deleted to declutter
    # the queue would otherwise force the next re-sync to scan everything again.
    # Only a history-complete job leaves a watermark: a completed run, or a
    # continuous job past backfill (now listening). A partial sharded run can have
    # gaps below its highest copied id, so its id is not a safe resume point.
    job = get_by_id(job_id)
    if job is not None and (job.status == "completed" or job.backfill_done):
        from app.repositories import channel_sync_repo
        channel_sync_repo.record_from_job(job)

    conn = db.get_connection()
    # Chunks have no FK to jobs (copied_messages taught us that a FK here costs us
    # the stats), so they have to be cleared explicitly or they outlive the job.
    conn.execute("DELETE FROM job_chunks WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM job_errors WHERE job_id = ?", (job_id,))
    cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    return cur.rowcount > 0


def get_queue_position(job_id: int) -> int:
    """Return 1-based position of this pending job in the queue (1 = next to run)."""
    conn = db.get_connection()
    # Get the submitted_at of this job
    target = conn.execute(
        "SELECT COALESCE(submitted_at, created_at) as sort_key FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not target:
        return 1
    row = conn.execute(
        """SELECT COUNT(*) as cnt FROM jobs
           WHERE status = 'pending'
             AND COALESCE(continuous,0) = 0
             AND COALESCE(submitted_at, created_at) <= ?""",
        (target["sort_key"],),
    ).fetchone()
    return row["cnt"] if row else 1


def count_by_status() -> dict[str, int]:
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
    ).fetchall()
    return {r["status"]: r["cnt"] for r in rows}


# ── Copied messages helpers ────────────────────────────────────────────────────

def save_report_url(job_id: int, url: str) -> None:
    conn = db.get_connection()
    conn.execute(
        "UPDATE jobs SET report_url = ? WHERE id = ?", (url, job_id)
    )
    conn.commit()


def get_report_messages(job_id: int) -> list[dict]:
    """
    Return failed + non-routine-skipped messages for report generation.
    Excludes: blocked_word, empty_message, duplicate, content_type:* — all expected behavior.
    """
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT source_message_id, status, skip_reason
           FROM copied_messages
           WHERE job_id = ?
             AND (
               status = 'failed'
               OR (
                 status = 'skipped'
                 AND (skip_reason IS NULL
                      OR (skip_reason NOT IN ('blocked_word', 'empty_message', 'duplicate')
                          AND skip_reason NOT LIKE 'content_type:%'))
               )
             )
           ORDER BY source_message_id
           LIMIT 5000""",
        (job_id,),
    ).fetchall()
    return [
        {"msg_id": r["source_message_id"], "status": r["status"], "reason": r["skip_reason"]}
        for r in rows
    ]


def get_failed_source_ids(job_id: int, limit: int = 500) -> list[int]:
    """
    Source message IDs this job could not copy — the input to the end-of-job
    retry pass. Capped so a badly broken run doesn't turn its own tail into a
    second full pass.
    """
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT source_message_id FROM copied_messages
           WHERE job_id = ? AND status = 'failed'
           ORDER BY source_message_id
           LIMIT ?""",
        (job_id, limit),
    ).fetchall()
    return [r["source_message_id"] for r in rows]


def get_transfer_stats() -> dict:
    """Return copied-message counts for the last hour, since midnight Israel time,
    last 24h, and since the very first transfer.

    processed_at is stored as UTC in SQLite. Cutoffs are computed in Python using the
    real Israel timezone (Asia/Jerusalem) so DST transitions are handled correctly.

    The all-time figure is a true lifetime total: copied_messages rows deliberately
    have no FK to jobs, so deleting a job does not erase its transfers from history.
    """
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo
    _IL = ZoneInfo("Asia/Jerusalem")

    now_utc = datetime.now(timezone.utc)
    # Midnight today in Israel time, converted back to UTC
    now_il = now_utc.astimezone(_IL)
    midnight_il = now_il.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = midnight_il.astimezone(timezone.utc)

    fmt = "%Y-%m-%d %H:%M:%S"
    cutoff_hour     = (now_utc - timedelta(hours=1)).strftime(fmt)
    cutoff_midnight = midnight_utc.strftime(fmt)
    cutoff_24h      = (now_utc - timedelta(hours=24)).strftime(fmt)

    conn = db.get_connection()
    row = conn.execute(
        """SELECT
             COUNT(CASE WHEN processed_at >= ? THEN 1 END) AS last_hour,
             COUNT(CASE WHEN processed_at >= ? THEN 1 END) AS since_midnight,
             COUNT(CASE WHEN processed_at >= ? THEN 1 END) AS last_24h,
             COUNT(*)                                      AS all_time,
             MIN(processed_at)                             AS first_at
           FROM copied_messages
           WHERE status = 'copied'""",
        (cutoff_hour, cutoff_midnight, cutoff_24h),
    ).fetchone()

    # Grouped on copied_messages.userbot_id — the account is stamped on the row at
    # transfer time. Deriving it from jobs.assigned_userbot_id would lose every
    # completed job, because the assignment is cleared the moment a job ends.
    rows = conn.execute(
        """SELECT
             cm.userbot_id,
             COUNT(CASE WHEN cm.processed_at >= ? THEN 1 END) AS last_hour,
             COUNT(CASE WHEN cm.processed_at >= ? THEN 1 END) AS since_midnight,
             COUNT(CASE WHEN cm.processed_at >= ? THEN 1 END) AS last_24h,
             COUNT(*)                                         AS all_time
           FROM copied_messages cm
           WHERE cm.status = 'copied'
           GROUP BY cm.userbot_id""",
        (cutoff_hour, cutoff_midnight, cutoff_24h),
    ).fetchall()

    # Key 0 collects rows copied before per-account attribution existed.
    userbots_stats = {}
    for r in rows:
        uid = r["userbot_id"] or 0
        userbots_stats[uid] = {
            "last_hour": r["last_hour"] or 0,
            "since_midnight": r["since_midnight"] or 0,
            "last_24h": r["last_24h"] or 0,
            "all_time": r["all_time"] or 0,
        }

    # Hyper transfers consume the same account quota (see
    # get_daily_count_for_userbot) — add them so the displayed numbers match
    # what the daily cap is actually enforced against.
    hyper_rows = conn.execute(
        """SELECT
             userbot_id,
             COUNT(CASE WHEN transferred_at >= ? THEN 1 END) AS last_hour,
             COUNT(CASE WHEN transferred_at >= ? THEN 1 END) AS since_midnight,
             COUNT(CASE WHEN transferred_at >= ? THEN 1 END) AS last_24h,
             COUNT(*)                                        AS all_time,
             MIN(transferred_at)                             AS first_at
           FROM hyper_transfers
           GROUP BY userbot_id""",
        (cutoff_hour, cutoff_midnight, cutoff_24h),
    ).fetchall()

    hyper_totals = {"last_hour": 0, "since_midnight": 0, "last_24h": 0, "all_time": 0}
    hyper_first_at = None
    for r in hyper_rows:
        uid = r["userbot_id"] or 0
        stats = userbots_stats.setdefault(
            uid, {"last_hour": 0, "since_midnight": 0, "last_24h": 0, "all_time": 0}
        )
        for key in hyper_totals:
            stats[key] += r[key] or 0
            hyper_totals[key] += r[key] or 0
        if r["first_at"] and (hyper_first_at is None or r["first_at"] < hyper_first_at):
            hyper_first_at = r["first_at"]

    if not row:
        return {
            "last_hour": 0, "since_midnight": 0, "last_24h": 0, "all_time": 0,
            "first_at": None, "userbots": userbots_stats,
        }
    # Both timestamp columns are UTC strings in the same format, so the earlier
    # of the two is simply the smaller string.
    first_candidates = [t for t in (row["first_at"], hyper_first_at) if t]
    return {
        "last_hour":      (row["last_hour"] or 0) + hyper_totals["last_hour"],
        "since_midnight": (row["since_midnight"] or 0) + hyper_totals["since_midnight"],
        "last_24h":       (row["last_24h"] or 0) + hyper_totals["last_24h"],
        "all_time":       (row["all_time"] or 0) + hyper_totals["all_time"],
        "first_at":       min(first_candidates) if first_candidates else None,
        "userbots":       userbots_stats,
    }


def get_daily_count_for_userbot(userbot_id: int) -> int:
    """
    Messages this account has copied since midnight Israel time.

    Telegram's limits are per-account, so this — not the global total — is what
    the daily cap is checked against.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    _IL = ZoneInfo("Asia/Jerusalem")

    now_il = datetime.now(timezone.utc).astimezone(_IL)
    midnight_utc = now_il.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    cutoff = midnight_utc.strftime("%Y-%m-%d %H:%M:%S")

    conn = db.get_connection()
    row = conn.execute(
        """SELECT COUNT(*) AS cnt FROM copied_messages
           WHERE status = 'copied' AND userbot_id = ? AND processed_at >= ?""",
        (userbot_id, cutoff),
    ).fetchone()
    bulk = row["cnt"] if row else 0

    # Hyper transfers consume the same account quota but live in their own table
    # (copied_messages can't key them). Count them together so a single cap covers
    # all of this account's send activity.
    hyper_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM hyper_transfers WHERE userbot_id = ? AND transferred_at >= ?",
        (userbot_id, cutoff),
    ).fetchone()
    hyper = hyper_row["cnt"] if hyper_row else 0

    return bulk + hyper


def backfill_userbot_attribution(userbot_id: int) -> int:
    """
    Stamp historical rows with the given account.

    Everything copied before multi-account support came from the single .env
    session, so attributing the unlabelled backlog to the default account is
    accurate. One-time: after this, every new row is stamped at write time.
    """
    conn = db.get_connection()
    cur = conn.execute(
        "UPDATE copied_messages SET userbot_id = ? WHERE userbot_id IS NULL",
        (userbot_id,),
    )
    conn.commit()
    return cur.rowcount


def get_max_copied_source_id(job_id: int) -> int:
    """
    Highest source message id this job ever recorded — copied, skipped or failed.

    Every id at or below it has been accounted for, so it is the true "processed
    up to here" watermark even for a sharded job, whose job-wide last_processed_id
    is never maintained (each chunk keeps its own). Returns 0 if nothing was
    recorded.
    """
    conn = db.get_connection()
    row = conn.execute(
        "SELECT MAX(source_message_id) AS m FROM copied_messages WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return row["m"] if row and row["m"] else 0


def get_safe_watermark_source_id(job_id: int) -> int:
    """
    Highest source id that is safe to skip on a future re-sync.

    Unlike get_max_copied_source_id, this stops just below the *first* failed
    message: a watermark must guarantee everything at or below it was actually
    delivered, and a failed row was not. Skipping past it would drop that message
    forever. Returns the full processed max when nothing failed, or 0 if nothing
    was recorded (or the very first message failed).
    """
    conn = db.get_connection()
    row = conn.execute(
        "SELECT MIN(source_message_id) AS m FROM copied_messages "
        "WHERE job_id = ? AND status = 'failed'",
        (job_id,),
    ).fetchone()
    first_failed = row["m"] if row and row["m"] else None
    if first_failed is None:
        return get_max_copied_source_id(job_id)
    return max(first_failed - 1, 0)


def seed_checkpoint(job_id: int, last_processed_id: int) -> None:
    """
    Seed a fresh job's resume point from a saved sync watermark.

    Only meaningful before the job has run: the copy engine reads
    last_processed_id as its starting min_id, so this makes the job continue past
    history already synced instead of scanning it again. Guarded to jobs that have
    not started, so it can never rewind a job already in progress.
    """
    conn = db.get_connection()
    conn.execute(
        """UPDATE jobs SET last_processed_id = ?, last_updated_at = datetime('now')
           WHERE id = ? AND last_processed_id IS NULL AND status IN ('draft','pending')""",
        (last_processed_id, job_id),
    )
    conn.commit()


def is_message_processed(job_id: int, source_message_id: int) -> bool:
    """Single-message dedup check — avoids loading the whole set for long-lived jobs."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT 1 FROM copied_messages WHERE job_id = ? AND source_message_id = ? LIMIT 1",
        (job_id, source_message_id),
    ).fetchone()
    return row is not None


def get_copied_source_ids(
    job_id: int,
    id_from: Optional[int] = None,
    id_to: Optional[int] = None,
) -> set[int]:
    """
    Source message IDs already processed for this job.

    Pass id_from/id_to when running one chunk of a sharded job: only that slice is
    needed, and loading a large job's whole history for every chunk would be waste.
    """
    conn = db.get_connection()
    if id_from is None or id_to is None:
        rows = conn.execute(
            "SELECT source_message_id FROM copied_messages WHERE job_id = ?", (job_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT source_message_id FROM copied_messages
               WHERE job_id = ? AND source_message_id BETWEEN ? AND ?""",
            (job_id, id_from, id_to),
        ).fetchall()
    return {r["source_message_id"] for r in rows}


def record_copied_message(
    job_id: int,
    source_message_id: int,
    dest_message_id: Optional[int],
    status: str,
    skip_reason: Optional[str] = None,
    userbot_id: Optional[int] = None,
) -> None:
    conn = db.get_connection()
    # On conflict the row moves to the account and day of the *latest* send —
    # a re-send is real quota-consuming activity, so that is the correct
    # attribution for the daily counters.
    conn.execute(
        """INSERT INTO copied_messages
           (job_id, source_message_id, dest_message_id, status, skip_reason, userbot_id)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(job_id, source_message_id) DO UPDATE SET
               dest_message_id = excluded.dest_message_id,
               status = excluded.status,
               skip_reason = excluded.skip_reason,
               userbot_id = COALESCE(excluded.userbot_id, copied_messages.userbot_id),
               processed_at = datetime('now')""",
        (job_id, source_message_id, dest_message_id, status, skip_reason, userbot_id),
    )
    conn.commit()
