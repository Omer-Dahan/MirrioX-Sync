"""Business logic for job lifecycle. Enforces rules, calls repos."""
from __future__ import annotations

import logging
from typing import Optional

from app.models import DEFAULT_CONTENT_TYPES, Job, JobError
from app.repositories import job_repo, source_repo

logger = logging.getLogger(__name__)


def create_draft_job(
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
    """Create a job in draft state. Raises JobError on invalid input."""
    src = source_repo.get_source_by_id(source_id)
    if src is None:
        raise JobError("מקור לא קיים")

    dest_ids = destination_ids or [destination_id]
    _validate_destinations(src, dest_ids)

    # `mode` and `continuous` are orthogonal: mode says which history to copy
    # first, continuous says to keep listening once that history is done.
    _validate_mode_params(mode, date_from, date_to, id_from, id_to, single_message_id)

    job = job_repo.create(
        name=name,
        source_id=source_id,
        destination_id=dest_ids[0],
        destination_ids=dest_ids,
        mode=mode,
        date_from=date_from,
        date_to=date_to,
        id_from=id_from,
        id_to=id_to,
        single_message_id=single_message_id,
        use_blocked_words=use_blocked_words,
        group_media=group_media,
        copy_text=copy_text,
        content_types=content_types,
        created_by=created_by,
        continuous=continuous,
        allowed_userbot_ids=allowed_userbot_ids,
    )

    # Resume from a saved watermark: if this exact pair was synced before (even by
    # a job since deleted), continue past that history instead of scanning it
    # again. Only for full-history single-destination jobs — the same shape that
    # writes the watermark — so the seeded checkpoint always means "everything up
    # to here already reached this destination".
    if mode == "all" and len(dest_ids) == 1:
        from app.repositories import channel_sync_repo
        watermark = channel_sync_repo.get_watermark(source_id, dest_ids[0])
        if watermark > 0:
            job_repo.seed_checkpoint(job.id, watermark)
            logger.info(
                "Job #%d seeded from saved sync watermark #%d (source #%d → destination #%d)",
                job.id, watermark, source_id, dest_ids[0],
            )
            return _require_job(job.id)

    return job


def update_destinations(job_id: int, destination_ids: list[int]) -> Job:
    """Replace a draft/paused job's destinations. Revalidates, resets access exclusions."""
    job = _require_job(job_id)
    if job.status not in ("draft", "paused"):
        raise JobError("ניתן לערוך יעדים רק בטיוטה או בהשהיה")

    src = source_repo.get_source_by_id(job.source_id)
    if src is None:
        raise JobError("מקור לא קיים")
    _validate_destinations(src, destination_ids)

    job_repo.set_destinations(job_id, destination_ids)
    # Access facts may differ for the new destination set.
    job_repo.reset_exclusions(job_id)
    logger.info("Job #%d destinations set to %s", job_id, destination_ids)
    return _require_job(job_id)


def submit_job(job_id: int) -> Job:
    """Move a draft job to pending. Raises JobError if not allowed."""
    job = _require_job(job_id)

    if job.status != "draft":
        raise JobError("רק משימות בטיוטה ניתן להגיש")

    # active = job_repo.get_active_job()
    # if active and active.id != job_id:
    #     raise JobError(
    #         f"יש כבר משימה פעילה (#{active.id}: {active.name}). "
    #         "יש לבטל אותה תחילה."
    #     )

    job_repo.update_status(job_id, "pending")
    logger.info("Job #%d '%s' submitted → pending", job_id, job.name)
    return _require_job(job_id)


def cancel_job(job_id: int) -> Job:
    """Cancel a job (any non-terminal state). Raises JobError if terminal."""
    job = _require_job(job_id)
    if job.is_terminal():
        raise JobError("לא ניתן לבטל משימה שהסתיימה כבר")
    job_repo.update_status(job_id, "cancelled")
    logger.info("Job #%d '%s' cancelled", job_id, job.name)
    return _require_job(job_id)


def delete_job(job_id: int) -> None:
    """Delete a job. Only allowed when draft / terminal."""
    job = _require_job(job_id)
    if job.is_active():
        raise JobError("לא ניתן למחוק משימה פעילה. בטל אותה תחילה.")
    job_repo.delete(job_id)
    logger.info("Job #%d '%s' deleted", job_id, job.name)


def get_active_job() -> Optional[Job]:
    return job_repo.get_active_job()


def can_submit() -> bool:
    return True


# ── Helpers ────────────────────────────────────────────────────────────────────

def _require_job(job_id: int) -> Job:
    job = job_repo.get_by_id(job_id)
    if job is None:
        raise JobError(f"משימה #{job_id} לא נמצאה")
    return job


def _validate_destinations(src, destination_ids: list[int]) -> None:
    if not destination_ids:
        raise JobError("חייב לבחור לפחות יעד אחד")
    if len(set(destination_ids)) != len(destination_ids):
        raise JobError("יעד נבחר פעמיים")
    for did in destination_ids:
        dest = source_repo.get_destination_by_id(did)
        if dest is None:
            raise JobError("יעד לא קיים")
        if src.channel_ref == dest.channel_ref:
            raise JobError("מקור ויעד לא יכולים להיות אותו הערוץ")


def _validate_mode_params(
    mode: str,
    date_from: Optional[str],
    date_to: Optional[str],
    id_from: Optional[int],
    id_to: Optional[int],
    single_message_id: Optional[int],
) -> None:
    if mode == "date_range":
        if not date_from or not date_to:
            raise JobError("טווח תאריכים דורש תאריך התחלה וסיום")
    elif mode == "id_range":
        if id_from is None or id_to is None:
            raise JobError("טווח מזהים דורש מזהה התחלה וסיום")
        if id_from >= id_to:
            raise JobError("מזהה ההתחלה חייב להיות קטן ממזהה הסיום")
    elif mode == "single_id":
        if single_message_id is None:
            raise JobError("מצב הודעה בודדת דורש מזהה הודעה")
    elif mode != "all":
        raise JobError(f"מצב לא מוכר: {mode}")
