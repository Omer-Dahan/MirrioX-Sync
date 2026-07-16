"""
Tests for the per-job userbot allow-list (which accounts may run a job).

Run manually:  python -m pytest tests/test_job_allowed_userbots.py -q
"""
from __future__ import annotations

import os
import tempfile

import pytest

from app import db
from app.repositories import job_repo, userbot_repo, source_repo


@pytest.fixture()
def fresh_db():
    """A throwaway SQLite database with the full schema and two active accounts."""
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    db.close()
    db.init(path)
    db.init_schema()

    u1 = userbot_repo.create("acc1", "+111", "sessions/s1", status="active", is_default=True)
    u2 = userbot_repo.create("acc2", "+222", "sessions/s2", status="active")
    src = source_repo.add_source("src", "src")
    dst = source_repo.add_destination("dst", "dst")

    yield {"u1": u1, "u2": u2, "src": src, "dst": dst}
    db.close()


def _pending_job(src, dst, allowed=None):
    job = job_repo.create(
        name="job",
        source_id=src.id,
        destination_id=dst.id,
        mode="all",
        allowed_userbot_ids=allowed,
    )
    job_repo.update_status(job.id, "pending")
    return job


def test_allowed_ids_parsed(fresh_db):
    job = _pending_job(fresh_db["src"], fresh_db["dst"], allowed=str(fresh_db["u2"].id))
    assert job_repo.get_by_id(job.id).allowed_ids() == {fresh_db["u2"].id}


def test_restricted_job_claimable_only_by_allowed_account(fresh_db):
    u1, u2 = fresh_db["u1"], fresh_db["u2"]
    _pending_job(fresh_db["src"], fresh_db["dst"], allowed=str(u2.id))

    # The barred account never claims it...
    assert job_repo.claim_next_job(u1.id) is None
    # ...while the allowed account does.
    claimed = job_repo.claim_next_job(u2.id)
    assert claimed is not None and claimed.assigned_userbot_id == u2.id


def test_unrestricted_job_claimable_by_any_account(fresh_db):
    _pending_job(fresh_db["src"], fresh_db["dst"], allowed=None)
    claimed = job_repo.claim_next_job(fresh_db["u1"].id)
    assert claimed is not None


def test_empty_allow_list_imposes_no_restriction(fresh_db):
    # An empty string must behave exactly like NULL (all accounts).
    _pending_job(fresh_db["src"], fresh_db["dst"], allowed="")
    assert job_repo.claim_next_job(fresh_db["u1"].id) is not None


# ── Editing a paused job (soft settings) ─────────────────────────────────────

def test_edit_flags_preserve_copy_progress(fresh_db):
    """Editing soft settings must never touch the checkpoint or the copied history."""
    job = _pending_job(fresh_db["src"], fresh_db["dst"])
    job_repo.pause_job(job.id)
    job_repo.add_progress(job.id, copied=500, last_processed_id=1234)

    job_repo.update_flags(job.id, use_blocked_words=False, copy_text=False, continuous=True)
    job_repo.set_content_types(job.id, "image,video")
    job_repo.set_allowed_userbots(job.id, str(fresh_db["u1"].id))

    edited = job_repo.get_by_id(job.id)
    assert edited.use_blocked_words is False
    assert edited.copy_text is False
    assert edited.continuous is True
    assert edited.content_types == "image,video"
    assert edited.allowed_ids() == {fresh_db["u1"].id}
    # The whole point of editing over cancel+recreate: progress survives.
    assert edited.last_processed_id == 1234
    assert edited.copied_count == 500


def test_update_flags_ignores_unknown_columns(fresh_db):
    job = _pending_job(fresh_db["src"], fresh_db["dst"])
    # A key outside the whitelist must be silently ignored, not injected into SQL.
    job_repo.update_flags(job.id, status="running", group_media=False)
    edited = job_repo.get_by_id(job.id)
    assert edited.group_media is False
    assert edited.status == "pending"  # untouched — 'status' is not editable here


def test_set_allowed_userbots_clear(fresh_db):
    job = _pending_job(fresh_db["src"], fresh_db["dst"], allowed=str(fresh_db["u2"].id))
    job_repo.set_allowed_userbots(job.id, None)
    assert job_repo.get_by_id(job.id).allowed_ids() == set()
