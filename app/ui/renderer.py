"""
Assembles the text + keyboard for each bot screen.
Every render_* function returns (text, InlineKeyboardMarkup).
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from telegram import InlineKeyboardMarkup

from app.ui import texts, keyboards
from app.repositories import source_repo, filter_repo, state_repo, channel_access_repo

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
    dsts = [source_repo.get_destination_by_id(i) for i in job.destination_id_list()]
    queue_pos = job_repo.get_queue_position(job_id) if job.status == "pending" else None
    return texts.job_detail_text(job, src, dsts, queue_pos), keyboards.kb_job_detail(job)


def render_job_errors(job_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import job_repo, job_error_repo

    job = job_repo.get_by_id(job_id)
    if job is None:
        return texts.error_text(f"משימה #{job_id} לא נמצאה"), keyboards.kb_error_back("jobs")

    size = texts.JOB_ERRORS_PAGE_SIZE
    total = job_error_repo.count(job_id)
    total_pages = max(1, (total + size - 1) // size)
    # Errors are pruned and can be cleared, so a stale page number has to be clamped.
    page = max(0, min(page, total_pages - 1))
    entries = job_error_repo.page(job_id, offset=page * size, limit=size)
    return (
        texts.job_errors_text(job, entries, page=page, total=total),
        keyboards.kb_job_errors(job_id, page, total_pages),
    )


def render_job_edit(job_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import job_repo, userbot_repo
    job = job_repo.get_by_id(job_id)
    if job is None:
        return texts.error_text(f"משימה #{job_id} לא נמצאה"), keyboards.kb_error_back("jobs")
    word_count = filter_repo.count()
    accounts_str = texts.job_allowed_accounts_str(job)
    multi_account = userbot_repo.count_active() > 1
    return (
        texts.job_edit_text(job, word_count, accounts_str),
        keyboards.kb_job_edit(job, multi_account=multi_account),
    )


def render_job_edit_accounts(job_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import job_repo, userbot_repo
    job = job_repo.get_by_id(job_id)
    if job is None:
        return texts.error_text(f"משימה #{job_id} לא נמצאה"), keyboards.kb_error_back("jobs")
    active = userbot_repo.get_active()
    # An empty allow-list means "all accounts" — show every active account ticked.
    selected = job.allowed_ids() or {u.id for u in active}
    return (
        texts.job_edit_accounts_text(job),
        keyboards.kb_job_edit_userbots(job_id, active, selected),
    )


def render_job_edit_destinations(job_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import job_repo
    job = job_repo.get_by_id(job_id)
    if job is None:
        return texts.error_text(f"משימה #{job_id} לא נמצאה"), keyboards.kb_error_back("jobs")
    dests = source_repo.get_all_destinations()
    selected = set(job.destination_id_list())
    return (
        texts.job_edit_destinations_text(job),
        keyboards.kb_job_edit_dest_list(job_id, dests, selected),
    )


def render_job_edit_content_types(job_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.models import DEFAULT_CONTENT_TYPES
    from app.repositories import job_repo
    job = job_repo.get_by_id(job_id)
    if job is None:
        return texts.error_text(f"משימה #{job_id} לא נמצאה"), keyboards.kb_error_back("jobs")
    selected = {p.strip() for p in (job.content_types or DEFAULT_CONTENT_TYPES).split(",") if p.strip()}
    return (
        texts.job_edit_content_types_text(job),
        keyboards.kb_job_edit_content_types(job_id, selected),
    )


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
    access = channel_access_repo.get_report(channel_access_repo.KIND_SOURCE, source_id)
    return texts.source_detail_text(src, access), keyboards.kb_source_detail(source_id)


def render_dest_list(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    dests = source_repo.get_all_destinations()
    return texts.dest_list_text(dests), keyboards.kb_dest_list(dests, page=page)


def render_dest_detail(dest_id: int) -> tuple[str, InlineKeyboardMarkup]:
    dest = source_repo.get_destination_by_id(dest_id)
    if dest is None:
        return texts.error_text("יעד לא נמצא"), keyboards.kb_error_back("destinations")
    access = channel_access_repo.get_report(channel_access_repo.KIND_DEST, dest_id)
    return texts.dest_detail_text(dest, access), keyboards.kb_dest_detail(dest_id)


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


def render_userbot_run_menu(userbot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import userbot_repo
    ub = userbot_repo.get_by_id(userbot_id)
    if ub is None:
        return texts.error_text("חשבון לא נמצא"), keyboards.kb_error_back("userbots")
    return texts.run_menu_text(ub), keyboards.kb_userbot_run_menu(userbot_id)


def render_scripts_for_userbot(userbot_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Script library shown in the context of running on a specific account."""
    from app.repositories import userbot_repo, script_repo
    ub = userbot_repo.get_by_id(userbot_id)
    if ub is None:
        return texts.error_text("חשבון לא נמצא"), keyboards.kb_error_back("userbots")
    scripts = script_repo.list_scripts()
    return (
        texts.scripts_list_text(scripts, ub=ub),
        keyboards.kb_scripts_list(scripts, ub_id=userbot_id, page=page),
    )


def render_scripts_list(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Global script library management (reached from Settings)."""
    from app.repositories import script_repo
    scripts = script_repo.list_scripts()
    return texts.scripts_list_text(scripts), keyboards.kb_scripts_list(scripts, page=page)


def render_script_detail(script_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import script_repo
    script = script_repo.get_script(script_id)
    if script is None:
        return texts.error_text("סקריפט לא נמצא"), keyboards.kb_error_back("settings")
    return texts.script_detail_text(script), keyboards.kb_script_detail(script_id)


def render_script_confirm_delete(script_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import script_repo
    script = script_repo.get_script(script_id)
    if script is None:
        return texts.error_text("סקריפט לא נמצא"), keyboards.kb_error_back("settings")
    return (
        texts.script_confirm_delete_text(script["name"]),
        keyboards.kb_script_confirm_delete(script_id),
    )


def render_hyper_account_list() -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import userbot_repo, hyper_repo
    userbots = userbot_repo.get_all()
    statuses = {}
    for ub in userbots:
        cfg = hyper_repo.get_config(ub.id)
        statuses[ub.id] = bool(cfg and cfg["enabled"] and cfg["destination_id"])
    return texts.hyper_account_list_text(userbots, statuses), keyboards.kb_hyper_account_list(userbots, statuses)


def render_hyper_menu(acc_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import userbot_repo, hyper_repo
    ub = userbot_repo.get_by_id(acc_id)
    if ub is None:
        return texts.error_text("חשבון לא נמצא"), keyboards.kb_error_back("userbots")
    hyper_repo.ensure_config(acc_id)
    cfg = hyper_repo.get_config(acc_id)
    filters = hyper_repo.get_filters(acc_id)
    dsts = []
    for dest_id in hyper_repo.get_destination_ids(acc_id):
        rec = source_repo.get_destination_by_id(dest_id)
        if rec is not None:
            dsts.append(rec)
    queued = hyper_repo.queue_count(acc_id)
    return texts.hyper_menu_text(ub, cfg, dsts, queued), keyboards.kb_hyper_menu(acc_id, cfg, dsts, filters)


def render_hyper_type(acc_id: int, media_type: str) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import userbot_repo, hyper_repo
    ub = userbot_repo.get_by_id(acc_id)
    if ub is None:
        return texts.error_text("חשבון לא נמצא"), keyboards.kb_error_back("userbots")
    rule = hyper_repo.get_filter(acc_id, media_type)
    return texts.hyper_type_text(ub, media_type, rule), keyboards.kb_hyper_type(acc_id, media_type, rule)


def render_hyper_dst_picker(acc_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.repositories import userbot_repo, hyper_repo
    ub = userbot_repo.get_by_id(acc_id)
    if ub is None:
        return texts.error_text("חשבון לא נמצא"), keyboards.kb_error_back("userbots")
    dests = source_repo.get_all_destinations()
    selected = set(hyper_repo.get_destination_ids(acc_id))
    return texts.hyper_dst_picker_text(ub), keyboards.kb_hyper_dst_picker(acc_id, dests, selected)


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
