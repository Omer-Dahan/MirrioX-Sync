"""
Tests that the displayed per-account daily counter matches the enforcement
counter (both must include hyper transfers).

Run manually:  python -m pytest tests/test_transfer_stats_hyper.py -q
"""
from __future__ import annotations

import os
import tempfile

import pytest

from app import db
from app.repositories import job_repo, source_repo, userbot_repo


@pytest.fixture()
def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    db.close()
    db.init(path)
    db.init_schema()

    ub = userbot_repo.create("acc1", "+111", "sessions/s1", status="active", is_default=True)
    src = source_repo.add_source("src", "src")
    dst = source_repo.add_destination("dst", "dst")
    job = job_repo.create(name="j", source_id=src.id, destination_id=dst.id, mode="all")

    yield {"ub": ub, "dst": dst, "job": job}
    db.close()


def _seed_hyper_transfer(userbot_id: int) -> None:
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO hyper_transfers (userbot_id) VALUES (?)", (userbot_id,)
    )
    conn.commit()


def test_display_stats_match_enforcement_count(fresh_db):
    ub, job = fresh_db["ub"], fresh_db["job"]

    for mid in range(1, 4):
        job_repo.record_copied_message(job.id, mid, None, "copied", userbot_id=ub.id)
    # Skipped rows must count for neither number.
    job_repo.record_copied_message(job.id, 100, None, "skipped", "duplicate", userbot_id=ub.id)
    for _ in range(2):
        _seed_hyper_transfer(ub.id)

    stats = job_repo.get_transfer_stats()
    shown = stats["userbots"][ub.id]["since_midnight"]
    enforced = job_repo.get_daily_count_for_userbot(ub.id)

    assert enforced == 5  # 3 bulk + 2 hyper
    assert shown == enforced
    assert stats["since_midnight"] == enforced


def test_all_time_total_survives_job_deletion(fresh_db):
    ub, job = fresh_db["ub"], fresh_db["job"]

    for mid in range(1, 4):
        job_repo.record_copied_message(job.id, mid, None, "copied", userbot_id=ub.id)
    job_repo.record_copied_message(job.id, 100, None, "skipped", "duplicate", userbot_id=ub.id)
    for _ in range(2):
        _seed_hyper_transfer(ub.id)

    stats = job_repo.get_transfer_stats()
    assert stats["all_time"] == 5  # 3 bulk + 2 hyper, skipped excluded
    assert stats["userbots"][ub.id]["all_time"] == 5
    assert stats["first_at"] is not None

    # The lifetime total is history, not a live view of the jobs table.
    job_repo.delete(job.id)
    assert job_repo.get_transfer_stats()["all_time"] == 5
