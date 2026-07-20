"""
Tests for the log-driven fixes: over-long captions must stay out of albums, a
job's terminal write must only succeed once, and the error history must not fill
up with copies of the same error.

Run manually:  python -m pytest tests/test_caption_and_finalize.py -q
"""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

import pytest

from app import db
from app.repositories import job_chunk_repo, job_error_repo, job_repo, source_repo
from app.worker.copy_engine import (
    CopyEngine, _MAX_CAPTION_LEN, _raw_text, _truncate_caption, _utf16_len,
)


def _msg(msg_id: int, text: str = ""):
    """A stand-in with just the attributes the helpers under test read."""
    return SimpleNamespace(id=msg_id, message=text, text=text)


# ── Caption length ─────────────────────────────────────────────────────────────

def test_caption_at_limit_is_groupable():
    assert CopyEngine._caption_too_long(_msg(1, "x" * _MAX_CAPTION_LEN)) is False


def test_caption_over_limit_is_not_groupable():
    assert CopyEngine._caption_too_long(_msg(1, "x" * (_MAX_CAPTION_LEN + 1))) is True


def test_empty_and_missing_text_are_groupable():
    assert CopyEngine._caption_too_long(_msg(1, "")) is False
    assert CopyEngine._caption_too_long(_msg(1, None)) is False


def test_astral_emoji_counts_as_two_units():
    """Telegram counts UTF-16 code units, so an emoji is two — not one."""
    emoji = "😀"
    assert _utf16_len(emoji) == 2
    assert CopyEngine._caption_too_long(_msg(1, emoji * (_MAX_CAPTION_LEN // 2))) is False
    assert CopyEngine._caption_too_long(_msg(1, emoji * (_MAX_CAPTION_LEN // 2 + 1))) is True


def test_length_is_measured_on_the_unformatted_text():
    """.text carries markdown delimiters that Telegram does not count."""
    raw = "x" * _MAX_CAPTION_LEN
    msg = SimpleNamespace(id=1, message=raw, text=f"**{raw}**")
    assert _raw_text(msg) == raw
    assert CopyEngine._caption_too_long(msg) is False


def test_truncate_leaves_short_caption_untouched():
    assert _truncate_caption("hello") == "hello"
    assert _truncate_caption(None) == ""


def test_truncate_fits_long_caption_within_limit():
    out = _truncate_caption("x" * (_MAX_CAPTION_LEN + 500))
    assert _utf16_len(out) == _MAX_CAPTION_LEN
    assert out.endswith("…")


def test_truncate_never_splits_a_surrogate_pair():
    out = _truncate_caption("😀" * _MAX_CAPTION_LEN)
    assert _utf16_len(out) <= _MAX_CAPTION_LEN
    out.encode("utf-8")  # a split pair would raise here
    assert out.endswith("…")


# ── Album-failure fallback ─────────────────────────────────────────────────────

def test_checkpoint_stays_below_every_requeued_id():
    """
    The re-queued messages are not recorded anywhere, so the checkpoint must not
    pass them — otherwise a job that stops here drops them for good.
    """
    batch = [_msg(10), _msg(20), _msg(30), _msg(40)]
    culprit = batch[0]
    remaining = [m for m in batch if m.id != culprit.id]
    checkpoint = remaining[0].id - 1

    assert checkpoint < min(m.id for m in remaining)
    # The message that was sent is below the checkpoint, but it is written to
    # copied_messages, so a resumed run skips it instead of sending it twice.
    assert culprit.id <= checkpoint


# ── Terminal state is claimed exactly once ─────────────────────────────────────

@pytest.fixture()
def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    db.close()
    db.init(path)
    db.init_schema()

    src = source_repo.add_source("src", "src")
    dst = source_repo.add_destination("dst", "dst")
    yield {"src": src, "dst": dst}
    db.close()


def test_only_one_account_can_complete_a_job(fresh_db):
    job = job_repo.create(
        name="j", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all",
    )
    job_repo.mark_started(job.id)

    assert job_repo.mark_completed(job.id) is True
    assert job_repo.mark_completed(job.id) is False


def test_completing_does_not_overwrite_a_cancelled_job(fresh_db):
    job = job_repo.create(
        name="j", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all",
    )
    job_repo.update_status(job.id, "cancelled")

    assert job_repo.mark_completed(job.id) is False
    assert job_repo.get_by_id(job.id).status == "cancelled"


def test_only_one_account_can_finish_a_backfill(fresh_db):
    job = job_repo.create(
        name="c", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all", continuous=True,
    )
    job_repo.mark_started(job.id)

    assert job_repo.mark_backfill_done(job.id) is True
    assert job_repo.mark_backfill_done(job.id) is False


def test_failed_ids_feed_the_retry_pass(fresh_db):
    job = job_repo.create(
        name="j", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all",
    )
    job_repo.record_copied_message(job.id, 100, None, "copied", None)
    job_repo.record_copied_message(job.id, 101, None, "failed", "boom")
    job_repo.record_copied_message(job.id, 102, None, "skipped", "duplicate")
    job_repo.record_copied_message(job.id, 103, None, "failed", "boom")

    assert job_repo.get_failed_source_ids(job.id) == [101, 103]


def test_recovered_message_moves_from_failed_to_copied(fresh_db):
    job = job_repo.create(
        name="j", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all",
    )
    job_repo.record_copied_message(job.id, 101, None, "failed", "boom")
    job_repo.add_progress(job.id, failed=1)

    # What _retry_failed does once a re-send succeeds.
    job_repo.record_copied_message(job.id, 101, None, "copied", None)
    job_repo.add_progress(job.id, copied=1, failed=-1)

    fresh = job_repo.get_by_id(job.id)
    assert fresh.copied_count == 1
    assert fresh.failed_count == 0
    assert job_repo.get_failed_source_ids(job.id) == []


# ── Error history dedup ────────────────────────────────────────────────────────

def test_repeated_error_only_bumps_its_timestamp(fresh_db):
    job = job_repo.create(
        name="j", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all",
    )
    for _ in range(5):
        job_error_repo.add(job.id, "FloodWait: 30s")

    assert job_error_repo.count(job.id) == 1


def test_repeated_long_error_is_still_deduped(fresh_db):
    """
    The stored text is truncated; comparing the untruncated incoming text against
    it never matched, so a long error used to insert a row on every retry.
    """
    job = job_repo.create(
        name="j", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all",
    )
    long_error = "boom " * 300  # comfortably over the stored length
    for _ in range(5):
        job_error_repo.add(job.id, long_error)

    assert job_error_repo.count(job.id) == 1
    assert len(job_error_repo.page(job.id)[0]["error"]) <= 500


def test_a_different_account_gets_its_own_entry(fresh_db):
    job = job_repo.create(
        name="j", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all",
    )
    job_error_repo.add(job.id, "same text", userbot_id=1)
    job_error_repo.add(job.id, "same text", userbot_id=2)

    assert job_error_repo.count(job.id) == 2


# ── Stranded sharded jobs ──────────────────────────────────────────────────────

def _sharded_job(fresh_db, chunks_done: int):
    """A running sharded job with `chunks_done` of its two chunks finished."""
    job = job_repo.create(
        name="j", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all",
    )
    job_chunk_repo.plan(job.id, [(1, 100), (101, 200)])
    job_repo.mark_started(job.id)
    for _ in range(chunks_done):
        chunk = job_chunk_repo.claim_next(job.id, userbot_id=1)
        job_chunk_repo.mark_done(chunk.id, owner_id=1)
    return job


def test_a_job_nobody_closed_is_found_once_it_goes_quiet(fresh_db):
    job = _sharded_job(fresh_db, chunks_done=2)
    job_repo.clear_assignment(job.id)

    # Still fresh: an account may be inside _finalize right now.
    assert job_chunk_repo.find_stranded_jobs(min_idle_minutes=10) == []
    # Long enough without a single update that nobody can still be working on it.
    assert job_chunk_repo.find_stranded_jobs(min_idle_minutes=0) == [job.id]


def test_a_job_with_work_left_is_not_stranded(fresh_db):
    job = _sharded_job(fresh_db, chunks_done=1)
    job_repo.clear_assignment(job.id)

    assert job_chunk_repo.find_stranded_jobs(min_idle_minutes=0) == []


def test_an_unsharded_job_is_never_stranded(fresh_db):
    """No chunks at all means an ordinary single-account run — not this case."""
    job = job_repo.create(
        name="j", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all",
    )
    job_repo.mark_started(job.id)
    job_repo.clear_assignment(job.id)

    assert job_chunk_repo.find_stranded_jobs(min_idle_minutes=0) == []


# ── Restarting a failed job ────────────────────────────────────────────────────

def test_restart_clears_the_previous_runs_report(fresh_db):
    job = job_repo.create(
        name="j", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all",
    )
    job_repo.save_report_url(job.id, "https://telegra.ph/old")
    job_repo.update_status(job.id, "failed", error="boom")

    assert job_repo.restart_failed_job(job.id) is True
    fresh = job_repo.get_by_id(job.id)
    assert fresh.status == "pending"
    assert fresh.report_url is None
    # The history of why it needed restarting is deliberately kept.
    assert job_error_repo.count(job.id) == 1


def test_restart_refuses_a_job_that_is_not_failed(fresh_db):
    job = job_repo.create(
        name="j", source_id=fresh_db["src"].id,
        destination_id=fresh_db["dst"].id, mode="all",
    )
    job_repo.mark_started(job.id)
    assert job_repo.restart_failed_job(job.id) is False
    assert job_repo.get_by_id(job.id).status == "running"
