"""
Assembles the text + keyboard for each bot screen.
Every render_* function returns (text, InlineKeyboardMarkup).
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from telegram import InlineKeyboardMarkup

from app.ui import texts, keyboards
from app.repositories import source_repo, filter_repo, state_repo

if TYPE_CHECKING:
    from app.models import Job, Source, Destination, Admin, BlockedWord, WorkerState


def render_main_menu() -> tuple[str, InlineKeyboardMarkup]:
    worker = state_repo.get_worker_state()
    from app.repositories import job_repo
    from app.repositories import scan_repo
    active = job_repo.get_active_job()
    active_scan = scan_repo.get_active_scan()
    active_delete_job = scan_repo.get_active_delete_job()
    text = texts.main_menu_text(worker.status, active, active_scan, active_delete_job)
    return text, keyboards.kb_main_menu()


def render_job_list(page: int = 0, telegram_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import job_repo
    jobs = job_repo.get_all(created_by=telegram_id)
    return texts.jobs_list_text(jobs), keyboards.kb_job_list(jobs, page=page)


def render_job_detail(job_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import job_repo
    job = job_repo.get_by_id(job_id)
    if job is None:
        return texts.error_text(f"משימה #{job_id} לא נמצאה"), keyboards.kb_error_back("jobs")
    src = source_repo.get_source_by_id(job.source_id)
    dst = source_repo.get_destination_by_id(job.destination_id)
    queue_pos = job_repo.get_queue_position(job_id) if job.status == "pending" else None
    return texts.job_detail_text(job, src, dst, queue_pos), keyboards.kb_job_detail(job)


def render_job_confirm_delete(job: "Job") -> tuple[str, InlineKeyboardMarkup]:
    return (
        texts.confirm_delete_job_text(job.name),
        keyboards.kb_confirm_delete_job(job.id),
    )


def render_job_confirm_cancel(job: "Job") -> tuple[str, InlineKeyboardMarkup]:
    return (
        texts.confirm_cancel_job_text(job.name),
        keyboards.kb_confirm_cancel_job(job.id),
    )


def render_source_list(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    sources = source_repo.get_all_sources()
    return texts.source_list_text(sources), keyboards.kb_source_list(sources, page=page)


def render_source_detail(source_id: int) -> tuple[str, InlineKeyboardMarkup]:
    src = source_repo.get_source_by_id(source_id)
    if src is None:
        return texts.error_text("מקור לא נמצא"), keyboards.kb_error_back("sources")
    return texts.source_detail_text(src), keyboards.kb_source_detail(source_id)


def render_dest_list(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    dests = source_repo.get_all_destinations()
    return texts.dest_list_text(dests), keyboards.kb_dest_list(dests, page=page)


def render_dest_detail(dest_id: int) -> tuple[str, InlineKeyboardMarkup]:
    dest = source_repo.get_destination_by_id(dest_id)
    if dest is None:
        return texts.error_text("יעד לא נמצא"), keyboards.kb_error_back("destinations")
    return texts.dest_detail_text(dest), keyboards.kb_dest_detail(dest_id)


def render_blocked_words(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    words = filter_repo.get_all()
    return texts.blocked_words_text(words), keyboards.kb_blocked_words(words, page=page)


def render_admin_list(bootstrap_ids: list[int], page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import admin_repo
    admins = admin_repo.get_all()
    return (
        texts.admin_list_text(admins, bootstrap_ids),
        keyboards.kb_admin_list(admins, bootstrap_ids, page=page),
    )


def render_settings() -> tuple[str, InlineKeyboardMarkup]:
    settings = state_repo.get_settings_dict()
    return texts.settings_text(settings), keyboards.kb_settings(settings)


def render_userbot_list(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import userbot_repo
    userbots = userbot_repo.get_all()
    return texts.userbot_list_text(userbots), keyboards.kb_userbot_list(userbots, page=page)


def render_userbot_detail(userbot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import userbot_repo
    ub = userbot_repo.get_by_id(userbot_id)
    if ub is None:
        return texts.error_text("חשבון לא נמצא"), keyboards.kb_error_back("userbots")
    return texts.userbot_detail_text(ub), keyboards.kb_userbot_detail(ub)


def render_userbot_confirm_remove(userbot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import userbot_repo
    ub = userbot_repo.get_by_id(userbot_id)
    if ub is None:
        return texts.error_text("חשבון לא נמצא"), keyboards.kb_error_back("userbots")
    return (
        texts.confirm_remove_userbot_text(ub.display()),
        keyboards.kb_confirm_remove_userbot(userbot_id),
    )


def render_transfer_stats() -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import job_repo, userbot_repo
    stats = job_repo.get_transfer_stats()
    userbots = userbot_repo.get_all()
    return texts.transfer_stats_text(stats, userbots), keyboards.kb_transfer_stats()


def render_scan_picker() -> tuple[str, InlineKeyboardMarkup]:
    dests = source_repo.get_all_destinations()
    return texts.scan_picker_text(dests), keyboards.kb_scan_picker(dests)


def render_scan_channel_menu(channel_ref: str, channel_title: str) -> tuple[str, InlineKeyboardMarkup]:
    return texts.scan_channel_menu_text(channel_title), keyboards.kb_scan_channel_menu(channel_ref)


def render_scan_history(channel_ref: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import scan_repo
    scans = scan_repo.get_scans_for_channel(channel_ref)
    if not scans:
        return texts.error_text("אין סריקות קודמות לערוץ זה"), keyboards.kb_scan_channel_menu(channel_ref)
    
    # if page out of bounds
    if page >= len(scans):
        page = len(scans) - 1
    elif page < 0:
        page = 0

    scan = scans[page]
    channel_name = scan.get("channel_title") or scan.get("channel_ref") or "—"
    has_dupes = (scan.get("duplicate_groups") or 0) > 0
    return (
        texts.scan_report_text(scan, channel_name),
        keyboards.kb_scan_history(scan["id"], scan["status"], has_dupes, scan.get("report_url"), channel_ref, page, len(scans)),
    )


def render_scan_report_by_id(scan_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import scan_repo
    scan = scan_repo.get_scan_by_id(scan_id)
    if scan is None:
        return texts.error_text("סריקה לא נמצאה"), keyboards.kb_error_back("scan")
    channel_name = scan.get("channel_title") or scan.get("channel_ref") or "—"
    has_dupes = (scan.get("duplicate_groups") or 0) > 0
    
    # Render with the old report view (for jobs list/active view)
    return (
        texts.scan_report_text(scan, channel_name),
        keyboards.kb_scan_report(scan_id, scan["status"], has_dupes, scan.get("report_url")),
    )


def render_confirm_delete_dupes_by_id(scan_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import scan_repo
    scan = scan_repo.get_scan_by_id(scan_id)
    wasted = (scan or {}).get("wasted_count", 0)
    return (
        texts.confirm_delete_dupes_text(wasted),
        keyboards.kb_confirm_delete_dupes(scan_id),
    )


def render_error(msg: str, back_target: str = "main") -> tuple[str, InlineKeyboardMarkup]:
    return texts.error_text(msg), keyboards.kb_error_back(back_target)


def render_wizard_step(
    step_text: str,
    partial: dict,
    keyboard: InlineKeyboardMarkup,
) -> tuple[str, InlineKeyboardMarkup]:
    header = texts.wizard_header(
        partial.get("_step", 1),
        partial.get("_total", 7),
        partial,
    )
    return f"{texts.TITLE_NEW_JOB}\n\n{header}\n\n{step_text}", keyboard
