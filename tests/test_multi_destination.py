"""
Tests for multi-destination jobs: the destination list column, cross-destination
dedup, claim eligibility, and destination-edit validation.

Run manually:  python -m pytest tests/test_multi_destination.py -q
"""
from __future__ import annotations

import os
import tempfile

import pytest

from app import db
from app.models import Job, JobError
from app.repositories import (
    channel_access_repo,
    dedup_repo,
    job_repo,
    source_repo,
    userbot_repo,
)
from app.services import job_service


@pytest.fixture()
def fresh_db():
    """A throwaway SQLite database with two accounts, one source and three destinations."""
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    db.close()
    db.init(path)
    db.init_schema()

    u1 = userbot_repo.create("acc1", "+111", "sessions/s1", status="active", is_default=True)
    u2 = userbot_repo.create("acc2", "+222", "sessions/s2", status="active")
    src = source_repo.add_source("src", "src")
    d1 = source_repo.add_destination("dst1", "dst1")
    d2 = source_repo.add_destination("dst2", "dst2")
    d3 = source_repo.add_destination("dst3", "dst3")

    yield {"u1": u1, "u2": u2, "src": src, "d1": d1, "d2": d2, "d3": d3}
    db.close()


# ── Job.destination_id_list ──────────────────────────────────────────────────

def _job_with(dest_ids_column, destination_id=7):
    return Job(
        id=1, name="j", source_id=1, destination_id=destination_id, mode="all",
        date_from=None, date_to=None, id_from=None, id_to=None,
        single_message_id=None, use_blocked_words=False, group_media=True,
        copy_text=True, content_types="file,image,text,video", report_url=None,
        status="draft", created_at="", started_at=None, completed_at=None,
        last_updated_at="", total_messages=0, copied_count=0, skipped_count=0,
        failed_count=0, last_processed_id=None, retry_count=0, max_retries=3,
        next_retry_at=None, error_message=None, destination_ids=dest_ids_column,
    )


def test_destination_id_list_falls_back_to_single_column():
    assert _job_with(None).destination_id_list() == [7]
    assert _job_with("").destination_id_list() == [7]


def test_destination_id_list_parses_and_preserves_order():
    assert _job_with("3,7,9", destination_id=3).destination_id_list() == [3, 7, 9]


# ── Cross-destination dedup ──────────────────────────────────────────────────

def test_exists_any_finds_key_in_any_listed_destination(fresh_db):
    d1, d2, d3 = fresh_db["d1"], fresh_db["d2"], fresh_db["d3"]
    dedup_repo.record(destination_id=d1.id, kind="text", dedup_key="k1")

    assert dedup_repo.exists_any([d1.id, d2.id], "k1")
    assert dedup_repo.exists_any([d2.id, d1.id], "k1")
    assert not dedup_repo.exists_any([d3.id], "k1")
    assert not dedup_repo.exists_any([], "k1")


# ── job_repo.create / set_destinations round-trip ────────────────────────────

def test_create_with_multiple_destinations(fresh_db):
    src, d1, d2 = fresh_db["src"], fresh_db["d1"], fresh_db["d2"]
    job = job_repo.create(
        name="j", source_id=src.id, destination_id=d1.id, mode="all",
        destination_ids=[d1.id, d2.id],
    )
    assert job.destination_id == d1.id
    assert job.destination_id_list() == [d1.id, d2.id]


def test_create_single_destination_stores_null_list(fresh_db):
    src, d1 = fresh_db["src"], fresh_db["d1"]
    job = job_repo.create(name="j", source_id=src.id, destination_id=d1.id, mode="all")
    assert job.destination_ids is None
    assert job.destination_id_list() == [d1.id]


def test_set_destinations_updates_primary_and_list(fresh_db):
    src, d1, d2, d3 = fresh_db["src"], fresh_db["d1"], fresh_db["d2"], fresh_db["d3"]
    job = job_repo.create(name="j", source_id=src.id, destination_id=d1.id, mode="all")

    job_repo.set_destinations(job.id, [d2.id, d3.id])
    fresh = job_repo.get_by_id(job.id)
    assert fresh.destination_id == d2.id
    assert fresh.destination_id_list() == [d2.id, d3.id]

    # Collapsing back to one destination clears the list column.
    job_repo.set_destinations(job.id, [d3.id])
    fresh = job_repo.get_by_id(job.id)
    assert fresh.destination_id == d3.id
    assert fresh.destination_ids is None


def test_claim_blocked_by_no_access_to_any_listed_destination(fresh_db):
    """A recorded lack of access to a secondary destination must block claiming."""
    u1, src, d1, d2 = fresh_db["u1"], fresh_db["src"], fresh_db["d1"], fresh_db["d2"]
    job = job_repo.create(
        name="j", source_id=src.id, destination_id=d1.id, mode="all",
        destination_ids=[d1.id, d2.id],
    )
    job_repo.update_status(job.id, "pending")

    channel_access_repo.record(
        channel_access_repo.KIND_DEST, d2.id, u1.id, has_access=False
    )
    assert job_repo.claim_next_job(u1.id) is None

    # The other account has no recorded lack of access — it claims normally.
    claimed = job_repo.claim_next_job(fresh_db["u2"].id)
    assert claimed is not None


def test_active_with_access_all_requires_every_destination(fresh_db):
    u1, u2 = fresh_db["u1"], fresh_db["u2"]
    src, d1, d2 = fresh_db["src"], fresh_db["d1"], fresh_db["d2"]

    for uid in (u1.id, u2.id):
        channel_access_repo.record(channel_access_repo.KIND_SOURCE, src.id, uid, True)
        channel_access_repo.record(channel_access_repo.KIND_DEST, d1.id, uid, True)
    # Only u1 can reach the second destination.
    channel_access_repo.record(channel_access_repo.KIND_DEST, d2.id, u1.id, True)
    channel_access_repo.record(channel_access_repo.KIND_DEST, d2.id, u2.id, False)

    assert channel_access_repo.active_with_access_all(src.id, [d1.id]) == {u1.id, u2.id}
    assert channel_access_repo.active_with_access_all(src.id, [d1.id, d2.id]) == {u1.id}


# ── job_service validation ───────────────────────────────────────────────────

def test_create_draft_job_rejects_duplicate_destinations(fresh_db):
    src, d1 = fresh_db["src"], fresh_db["d1"]
    with pytest.raises(JobError):
        job_service.create_draft_job(
            name="j", source_id=src.id, destination_id=d1.id, mode="all",
            destination_ids=[d1.id, d1.id],
        )


def test_create_draft_job_rejects_destination_equal_to_source(fresh_db):
    src = fresh_db["src"]
    same_as_source = source_repo.add_destination("src", "src")
    with pytest.raises(JobError):
        job_service.create_draft_job(
            name="j", source_id=src.id, destination_id=same_as_source.id, mode="all",
            destination_ids=[fresh_db["d1"].id, same_as_source.id],
        )


def test_update_destinations_only_for_draft_or_paused(fresh_db):
    src, d1, d2 = fresh_db["src"], fresh_db["d1"], fresh_db["d2"]
    job = job_service.create_draft_job(
        name="j", source_id=src.id, destination_id=d1.id, mode="all"
    )

    updated = job_service.update_destinations(job.id, [d1.id, d2.id])
    assert updated.destination_id_list() == [d1.id, d2.id]

    job_repo.update_status(job.id, "running")
    with pytest.raises(JobError):
        job_service.update_destinations(job.id, [d1.id])


def test_update_destinations_resets_exclusions(fresh_db):
    src, d1, d2 = fresh_db["src"], fresh_db["d1"], fresh_db["d2"]
    job = job_service.create_draft_job(
        name="j", source_id=src.id, destination_id=d1.id, mode="all"
    )
    job_repo.exclude_userbot(job.id, fresh_db["u1"].id)
    assert job_repo.get_by_id(job.id).excluded_ids()

    job_service.update_destinations(job.id, [d2.id])
    assert not job_repo.get_by_id(job.id).excluded_ids()
