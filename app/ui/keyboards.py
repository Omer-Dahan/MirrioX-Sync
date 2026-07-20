"""InlineKeyboardMarkup builders. All callback_data follows domain:id:action format.

All existing functions return InlineKeyboardMarkup (kept for renderer.py compatibility).
Use to_telethon(markup) to convert for Telethon bot sending.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import Button

from app.ui import texts

if TYPE_CHECKING:
    from app.models import Job, Source, Destination, BlockedWord, Admin


def to_telethon(markup: InlineKeyboardMarkup) -> list[list[Button]]:
    """Convert a PTB InlineKeyboardMarkup to a Telethon Button grid."""
    rows = []
    for row in markup.inline_keyboard:
        tg_row = []
        for btn in row:
            if btn.url:
                tg_row.append(Button.url(btn.text, btn.url))
            else:
                tg_row.append(Button.inline(btn.text, data=btn.callback_data or ""))
        rows.append(tg_row)
    return rows


_PAGE_SIZE = 8  # items per page; nav row added when list exceeds this


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _url_btn(label: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, url=url)


def _back(target: str) -> list[InlineKeyboardButton]:
    return [_btn(texts.BTN_BACK, f"menu:{target}")]


def _paged(items: list, page: int) -> tuple[list, int]:
    """Return (page_items, total_pages). If total_pages==1, no paging needed."""
    total = len(items)
    if total <= _PAGE_SIZE:
        return items, 1
    total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
    start = page * _PAGE_SIZE
    return items[start : start + _PAGE_SIZE], total_pages


def _nav_row(screen: str, page: int, total_pages: int) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(_btn("⬅️ הקודם", f"page:{screen}:{page - 1}"))
    if page < total_pages - 1:
        row.append(_btn("הבא ➡️", f"page:{screen}:{page + 1}"))
    return row


def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn(texts.BTN_JOBS, "menu:jobs"), _btn(texts.BTN_NEW_JOB, "job:new")],
        [_btn(texts.BTN_SOURCES, "menu:sources"), _btn(texts.BTN_DESTINATIONS, "menu:destinations")],
        [_btn(texts.BTN_BLOCKED_WORDS, "menu:filters"), _btn(texts.BTN_ADMINS, "menu:admins")],
        [_btn(texts.BTN_SETTINGS, "menu:settings"), _btn(texts.BTN_SCAN_DUPES_MENU, "menu:scan")],
        [_btn(texts.BTN_TRANSFER_STATS, "menu:stats")],
    ])


# ── Jobs ───────────────────────────────────────────────────────────────────────

def kb_job_list(jobs: list["Job"], page: int = 0) -> InlineKeyboardMarkup:
    page_jobs, total_pages = _paged(jobs, page)
    rows = []
    for job in page_jobs:
        icon = texts.job_status_icon(job)
        label = f"{job.name[:43]} {icon}"
        rows.append([_btn(label, f"job:{job.id}:view")])
    if total_pages > 1:
        rows.append(_nav_row("jobs", page, total_pages))
    rows.append([_btn(texts.BTN_NEW_JOB, "job:new"), _btn(texts.BTN_MAIN_MENU, "menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_job_detail(job: "Job") -> InlineKeyboardMarkup:
    rows = []

    if job.status == "draft":
        rows.append([_btn(texts.BTN_SUBMIT_JOB, f"job:{job.id}:submit")])
        rows.append([_btn(texts.BTN_EDIT_JOB, f"je:{job.id}:menu")])
        rows.append([_btn(texts.BTN_DELETE_JOB, f"job:{job.id}:confirm_delete")])

    if job.status in ("pending", "running", "waiting_retry"):
        pause_label = texts.BTN_STOP_LISTENING if job.continuous else texts.BTN_PAUSE_JOB
        rows.append([_btn(pause_label, f"job:{job.id}:pause")])
        rows.append([_btn(texts.BTN_CANCEL_JOB, f"job:{job.id}:confirm_cancel")])

    if job.status == "paused":
        resume_label = texts.BTN_START_LISTENING if job.continuous else texts.BTN_RESUME_JOB
        rows.append([_btn(resume_label, f"job:{job.id}:resume")])
        rows.append([_btn(texts.BTN_EDIT_JOB, f"je:{job.id}:menu")])
        rows.append([_btn(texts.BTN_DELETE_JOB, f"job:{job.id}:confirm_delete")])

    if job.is_terminal():
        # A failed job can be fixed (accounts, destinations, filters) and re-run
        # from its checkpoint.
        if job.status == "failed":
            rows.append([_btn(texts.BTN_RESTART_JOB, f"job:{job.id}:restart")])
            rows.append([_btn(texts.BTN_EDIT_JOB, f"je:{job.id}:menu")])
        rows.append([_btn(texts.BTN_DELETE_JOB, f"job:{job.id}:confirm_delete")])

    from app.repositories import job_error_repo

    err_count = job_error_repo.count(job.id)
    if err_count:
        rows.append([_btn(f"⚠️ שגיאות ({err_count:,})", f"job:{job.id}:errors")])

    if job.report_url:
        rows.append([_url_btn("📋 דוח שגיאות / דילוגים", job.report_url)])

    rows.append([
        _btn(texts.BTN_REFRESH, f"job:{job.id}:view"),
        _btn(texts.BTN_BACK, "menu:jobs"),
    ])
    return InlineKeyboardMarkup(rows)


def kb_job_errors(job_id: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("⬅️ הקודם", f"job:{job_id}:errors:{page - 1}"))
        if page < total_pages - 1:
            nav.append(_btn("הבא ➡️", f"job:{job_id}:errors:{page + 1}"))
        if nav:
            rows.append(nav)
    rows.append([
        _btn(texts.BTN_REFRESH, f"job:{job_id}:errors:{page}"),
        _btn(texts.BTN_BACK, f"job:{job_id}:view"),
    ])
    return InlineKeyboardMarkup(rows)


def kb_confirm_delete_job(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn(texts.BTN_YES_DELETE, f"job:{job_id}:delete"),
        _btn(texts.BTN_CANCEL, f"job:{job_id}:view"),
    ]])


def kb_confirm_cancel_job(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn(texts.BTN_YES_CANCEL, f"job:{job_id}:cancel"),
        _btn(texts.BTN_CANCEL, f"job:{job_id}:view"),
    ]])


# ── Job edit (draft / paused) ────────────────────────────────────────────────

def kb_job_edit(job: "Job", multi_account: bool = True) -> InlineKeyboardMarkup:
    """Edit menu for the 'soft' settings of a draft/paused job."""
    jid = job.id
    filter_btn = texts.BTN_FILTER_TOGGLE_ON if job.use_blocked_words else texts.BTN_FILTER_TOGGLE_OFF
    group_btn = texts.BTN_GROUP_TOGGLE_ON if job.group_media else texts.BTN_GROUP_TOGGLE_OFF
    text_btn = texts.BTN_TEXT_TOGGLE_ON if job.copy_text else texts.BTN_TEXT_TOGGLE_OFF
    cont_btn = texts.BTN_CONTINUOUS_ON if job.continuous else texts.BTN_CONTINUOUS_OFF
    rows = []
    if multi_account:
        rows.append([_btn(texts.BTN_EDIT_ACCOUNTS, f"je:{jid}:accounts")])
    rows.append([_btn(texts.BTN_EDIT_DESTS, f"je:{jid}:dests")])
    rows.append([_btn(texts.BTN_EDIT_CONTENT_TYPES, f"je:{jid}:types")])
    rows.append([_btn(filter_btn, f"je:{jid}:tgl_filter")])
    rows.append([_btn(group_btn, f"je:{jid}:tgl_group")])
    rows.append([_btn(text_btn, f"je:{jid}:tgl_text")])
    rows.append([_btn(cont_btn, f"je:{jid}:tgl_cont")])
    rows.append([_btn(texts.BTN_RESET_EXCLUSIONS, f"je:{jid}:reset_excl")])
    # Editing a failed job is a fix-and-rerun flow, so it ends with a restart.
    if job.status == "failed":
        rows.append([_btn(texts.BTN_SAVE_AND_RESTART, f"je:{jid}:restart")])
    rows.append([_btn(texts.BTN_BACK, f"job:{jid}:view")])
    return InlineKeyboardMarkup(rows)


def kb_job_edit_userbots(
    job_id: int, userbots: list, selected_ids: set[int] | None = None
) -> InlineKeyboardMarkup:
    """Multi-select of allowed accounts, editing an existing job."""
    if selected_ids is None:
        selected_ids = set()
    rows = []
    row: list = []
    for ub in userbots:
        check = "✅" if ub.id in selected_ids else "◻"
        row.append(_btn(f"{check} {ub.display()[:30]}", f"je:{job_id}:ub:{ub.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn(texts.BTN_WZD_ALL_ACCOUNTS, f"je:{job_id}:all_ubs")])
    rows.append([_btn(texts.BTN_BACK, f"je:{job_id}:menu")])
    return InlineKeyboardMarkup(rows)


def kb_job_edit_dest_list(
    job_id: int, dests: list, selected_ids: set[int] | None = None
) -> InlineKeyboardMarkup:
    """Multi-select of destinations, editing an existing job."""
    if selected_ids is None:
        selected_ids = set()
    rows = []
    row: list = []
    for dest in dests:
        check = "✅" if dest.id in selected_ids else "◻"
        row.append(_btn(f"{check} {dest.display()[:30]}", f"je:{job_id}:dst:{dest.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn(texts.BTN_BACK, f"je:{job_id}:menu")])
    return InlineKeyboardMarkup(rows)


def kb_job_edit_content_types(job_id: int, selected: set) -> InlineKeyboardMarkup:
    def chk(t: str) -> str:
        return "✅" if t in selected else "◻"
    rows = [
        [_btn(f"{chk('image')} 🖼 תמונות (ומדבקות)", f"je:{job_id}:type:image")],
        [_btn(f"{chk('video')} 🎬 סרטונים (וGIF)", f"je:{job_id}:type:video")],
        [_btn(f"{chk('file')} 📎 קבצים (מסמכים ואודיו)", f"je:{job_id}:type:file")],
        [_btn(f"{chk('text')} 💬 טקסט", f"je:{job_id}:type:text")],
        [_btn(texts.BTN_BACK, f"je:{job_id}:menu")],
    ]
    return InlineKeyboardMarkup(rows)


# ── Wizard ─────────────────────────────────────────────────────────────────────

def kb_wizard_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(texts.BTN_CANCEL, "job:cancel_wizard")]])


def kb_wizard_name_step() -> InlineKeyboardMarkup:
    """Name step: allow skipping to auto-generate name from channel names."""
    return InlineKeyboardMarkup([
        [_btn("⏭ דלג (שם אוטומטי)", "wzd:skip_name")],
        [_btn(texts.BTN_CANCEL, "job:cancel_wizard")],
    ])


def kb_wizard_source_list(
    sources: list["Source"], selected_ids: list[int] | None = None
) -> InlineKeyboardMarkup:
    if selected_ids is None:
        selected_ids = []
    rows = []
    row: list = []
    for src in sources:
        check = "✅" if src.id in selected_ids else "◻"
        label = f"{check} {src.display()[:30]}"
        row.append(_btn(label, f"wzd:toggle_src:{src.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if selected_ids:
        rows.append([_btn("✔ סיים בחירה", "wzd:done_sources")])
    rows.append([_btn(texts.BTN_ADD + " מקור", "wzd:add_source")])
    rows.append([_btn(texts.BTN_CANCEL, "job:cancel_wizard")])
    return InlineKeyboardMarkup(rows)


def kb_wizard_dest_list(
    dests: list["Destination"], selected_ids: list[int] | None = None
) -> InlineKeyboardMarkup:
    if selected_ids is None:
        selected_ids = []
    rows = []
    row: list = []
    for dest in dests:
        check = "✅" if dest.id in selected_ids else "◻"
        label = f"{check} {dest.display()[:30]}"
        row.append(_btn(label, f"wzd:dst:{dest.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if selected_ids:
        rows.append([_btn("✔ סיים בחירה", "wzd:done_dests")])
    rows.append([_btn(texts.BTN_ADD + " יעד", "wzd:add_dest")])
    rows.append([_btn(texts.BTN_CANCEL, "job:cancel_wizard")])
    return InlineKeyboardMarkup(rows)


def kb_wizard_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn(texts.MODE_LABELS["all"],        "wzd:mode:all")],
        [_btn(texts.MODE_LABELS["date_range"], "wzd:mode:date_range")],
        [_btn(texts.MODE_LABELS["id_range"],   "wzd:mode:id_range")],
        [_btn(texts.MODE_LABELS["single_id"],  "wzd:mode:single_id")],
        [_btn(texts.BTN_CANCEL, "job:cancel_wizard")],
    ])


def kb_wizard_content_types(selected: set) -> InlineKeyboardMarkup:
    def chk(t: str) -> str:
        return "✅" if t in selected else "◻"
    rows = [
        [_btn(f"{chk('image')} 🖼 תמונות (ומדבקות)", "wzd:toggle_type:image")],
        [_btn(f"{chk('video')} 🎬 סרטונים (וGIF)", "wzd:toggle_type:video")],
        [_btn(f"{chk('file')} 📎 קבצים (מסמכים ואודיו)", "wzd:toggle_type:file")],
        [_btn(f"{chk('text')} 💬 טקסט", "wzd:toggle_type:text")],
    ]
    if selected:
        rows.append([_btn("✔ המשך", "wzd:done_types")])
    rows.append([_btn(texts.BTN_CANCEL, "job:cancel_wizard")])
    return InlineKeyboardMarkup(rows)


def kb_wizard_summary(
    use_blocked_words: bool,
    group_media: bool,
    copy_text: bool,
    continuous: bool = False,
    accounts_label: str | None = None,
) -> InlineKeyboardMarkup:
    filter_btn_label = texts.BTN_FILTER_TOGGLE_ON if use_blocked_words else texts.BTN_FILTER_TOGGLE_OFF
    group_btn_label = texts.BTN_GROUP_TOGGLE_ON if group_media else texts.BTN_GROUP_TOGGLE_OFF
    text_btn_label = texts.BTN_TEXT_TOGGLE_ON if copy_text else texts.BTN_TEXT_TOGGLE_OFF
    continuous_btn_label = texts.BTN_CONTINUOUS_ON if continuous else texts.BTN_CONTINUOUS_OFF
    rows = [
        [_btn(texts.BTN_SAVE_DRAFT, "wzd:confirm")],
        [_btn(continuous_btn_label, "wzd:toggle_continuous")],
        [_btn(filter_btn_label, "wzd:toggle_filter")],
        [_btn(group_btn_label, "wzd:toggle_group")],
        [_btn(text_btn_label, "wzd:toggle_copy_text")],
    ]
    # Only offered when more than one account is active — with a single account
    # there is nothing to choose.
    if accounts_label is not None:
        rows.append([_btn(accounts_label, "wzd:accounts")])
    rows.append([_btn(texts.BTN_CANCEL, "job:cancel_wizard")])
    return InlineKeyboardMarkup(rows)


def kb_wizard_userbot_list(
    userbots: list, selected_ids: set[int] | None = None
) -> InlineKeyboardMarkup:
    """Multi-select of the accounts allowed to run the job. All selected = no limit."""
    if selected_ids is None:
        selected_ids = set()
    rows = []
    row: list = []
    for ub in userbots:
        check = "✅" if ub.id in selected_ids else "◻"
        row.append(_btn(f"{check} {ub.display()[:30]}", f"wzd:toggle_ub:{ub.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn(texts.BTN_WZD_ALL_ACCOUNTS, "wzd:all_ubs")])
    if selected_ids:
        rows.append([_btn(texts.BTN_WZD_ACCOUNTS_DONE, "wzd:done_accounts")])
    rows.append([_btn(texts.BTN_CANCEL, "job:cancel_wizard")])
    return InlineKeyboardMarkup(rows)


# ── Sources ────────────────────────────────────────────────────────────────────

def kb_source_list(sources: list["Source"], page: int = 0) -> InlineKeyboardMarkup:
    page_srcs, total_pages = _paged(sources, page)
    rows = []
    for src in page_srcs:
        label = src.display()[:50]
        rows.append([_btn(label, f"src:{src.id}:view")])
    if total_pages > 1:
        rows.append(_nav_row("sources", page, total_pages))
    rows.append([_btn(texts.BTN_ADD + " מקור", "src:new"), _btn(texts.BTN_MAIN_MENU, "menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_source_detail(source_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn(texts.BTN_REFRESH + " מידע", f"src:{source_id}:refresh_info")],
        [_btn(texts.BTN_DELETE, f"src:{source_id}:confirm_delete")],
        [_btn(texts.BTN_BACK, "menu:sources")],
    ])


def kb_scan_picker(dests: list["Destination"]) -> InlineKeyboardMarkup:
    rows = []
    for dest in dests:
        label = (dest.title or dest.name or dest.channel_ref)[:45]
        rows.append([_btn(f"📤 {label}", f"scan:dst:{dest.id}")])
    rows.append([_btn("✏️ הזן ידנית", "scan:manual")])
    rows.append([_btn(texts.BTN_MAIN_MENU, "menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_scan_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(texts.BTN_CANCEL, "menu:scan")]])


def kb_scan_channel_menu(channel_ref: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("דוח סריקות שבוצעו", f"scan:hist:{channel_ref}:0")],
        [_btn(texts.BTN_START_SCAN, f"scan:new:{channel_ref}")],
        [_btn(texts.BTN_BACK, "menu:scan")],
    ])


def kb_scan_history(scan_id: int, status: str, has_dupes: bool, report_url: str | None, channel_ref: str, page: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    
    if status in ("running", "pending"):
        rows.append([
            _btn(texts.BTN_REFRESH, f"scan:hist:{channel_ref}:{page}"),
            _btn(texts.BTN_STOP_SCAN, f"scan:stop_hist:{scan_id}:{page}:{channel_ref}"),
        ])
    elif status == "done" and has_dupes:
        rows.append([_btn("מחיקת כפולים", f"scan:confirm_delete:{scan_id}:{page}:{channel_ref}")])
        if report_url:
            rows.append([_url_btn("דוח כפולים", report_url)])
    
    rows.append([_btn(texts.BTN_DEL_SCAN, f"scan:confirm_del_scan:{scan_id}:{page}:{channel_ref}")])

    # Navigation for history
    nav_row = []
    if page < total - 1:
        nav_row.append(_btn("הבא ➡️", f"scan:hist:{channel_ref}:{page + 1}"))
    if page > 0:
        nav_row.append(_btn("⬅️ הקודם", f"scan:hist:{channel_ref}:{page - 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([_btn(texts.BTN_BACK, f"scan:menu_ref:{channel_ref}")])
    return InlineKeyboardMarkup(rows)


def kb_scan_report(scan_id: int, status: str, has_dupes: bool, report_url: str | None = None) -> InlineKeyboardMarkup:
    # Used primarily by the active jobs viewer
    rows = []
    if status in ("running", "pending"):
        rows.append([
            _btn(texts.BTN_REFRESH, f"scan:view:{scan_id}"),
            _btn(texts.BTN_STOP_SCAN, f"scan:stop:{scan_id}"),
        ])
    elif status == "done" and has_dupes:
        rows.append([_btn(texts.BTN_DELETE_DUPES, f"scan:confirm_delete:{scan_id}")])
    elif status == "failed":
        pass
    rows.append([_btn(texts.BTN_BACK, "menu:jobs")])
    return InlineKeyboardMarkup(rows)


def kb_confirm_reset_scan(scan_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn(texts.BTN_CONFIRM_RESET, f"scan:reset:{scan_id}"),
        _btn(texts.BTN_CANCEL, f"scan:view:{scan_id}"),
    ]])


def kb_confirm_delete_dupes(scan_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn(texts.BTN_YES_DELETE, f"scan:delete:{scan_id}"),
        _btn(texts.BTN_CANCEL, f"scan:view:{scan_id}"),
    ]])


def kb_confirm_delete_source(source_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn(texts.BTN_YES_DELETE, f"src:{source_id}:delete"),
        _btn(texts.BTN_CANCEL, f"src:{source_id}:view"),
    ]])


# ── Destinations ───────────────────────────────────────────────────────────────

def kb_dest_list(dests: list["Destination"], page: int = 0) -> InlineKeyboardMarkup:
    page_dests, total_pages = _paged(dests, page)
    rows = []
    for dest in page_dests:
        label = dest.display()[:50]
        rows.append([_btn(label, f"dst:{dest.id}:view")])
    if total_pages > 1:
        rows.append(_nav_row("destinations", page, total_pages))
    rows.append([_btn(texts.BTN_ADD + " יעד", "dst:new"), _btn(texts.BTN_MAIN_MENU, "menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_dest_detail(dest_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn(texts.BTN_REFRESH + " מידע", f"dst:{dest_id}:refresh_info")],
        [_btn(texts.BTN_DELETE, f"dst:{dest_id}:confirm_delete")],
        [_btn(texts.BTN_BACK, "menu:destinations")],
    ])


def kb_confirm_delete_dest(dest_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn(texts.BTN_YES_DELETE, f"dst:{dest_id}:delete"),
        _btn(texts.BTN_CANCEL, f"dst:{dest_id}:view"),
    ]])


# ── Filters ────────────────────────────────────────────────────────────────────

def kb_blocked_words(words: list["BlockedWord"], page: int = 0) -> InlineKeyboardMarkup:
    page_words, total_pages = _paged(words, page)
    rows = []
    for w in page_words:
        label = f"🗑 {w.word[:30]}"
        rows.append([_btn(label, f"flt:{w.id}:delete")])
    if total_pages > 1:
        rows.append(_nav_row("filters", page, total_pages))
    ctrl = [_btn(texts.BTN_ADD + " מילה", "flt:new")]
    if words:
        ctrl.append(_btn("🗑 מחק הכל", "flt:confirm_clear"))
    rows.append(ctrl)
    rows.append([_btn(texts.BTN_MAIN_MENU, "menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_confirm_clear_words() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn(texts.BTN_YES_CLEAR, "flt:clear"),
        _btn(texts.BTN_CANCEL, "menu:filters"),
    ]])


def kb_filter_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(texts.BTN_CANCEL, "menu:filters")]])


def kb_source_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(texts.BTN_CANCEL, "menu:sources")]])


def kb_dest_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(texts.BTN_CANCEL, "menu:destinations")]])


# ── Admins ─────────────────────────────────────────────────────────────────────

def kb_admin_list(admins: list["Admin"], bootstrap_ids: list[int], page: int = 0) -> InlineKeyboardMarkup:
    removable = [a for a in admins if a.telegram_id not in bootstrap_ids]
    page_admins, total_pages = _paged(removable, page)
    rows = []
    for a in page_admins:
        label = f"🗑 {a.username or str(a.telegram_id)}"
        rows.append([_btn(label, f"adm:{a.telegram_id}:confirm_remove")])
    if total_pages > 1:
        rows.append(_nav_row("admins", page, total_pages))
    rows.append([_btn(texts.BTN_ADD + " מנהל", "adm:new"), _btn(texts.BTN_MAIN_MENU, "menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_confirm_remove_admin(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn("✅ כן, הסר", f"adm:{telegram_id}:remove"),
        _btn(texts.BTN_CANCEL, "menu:admins"),
    ]])


def kb_admin_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(texts.BTN_CANCEL, "menu:admins")]])


# ── Transfer stats ─────────────────────────────────────────────────────────────

def kb_transfer_stats() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn(texts.BTN_REFRESH, "menu:stats")],
        [_btn(texts.BTN_MAIN_MENU, "menu:main")],
    ])


# ── Settings ───────────────────────────────────────────────────────────────────

def kb_settings(settings: dict[str, str]) -> InlineKeyboardMarkup:
    rows = []
    for key in texts.EDITABLE_SETTINGS:
        label = texts.SETTINGS_LABELS.get(key, key)
        val = settings.get(key, "—")
        rows.append([_btn(f"{label}: {val}", f"cfg:{key}")])
    for key, label in texts.TOGGLE_SETTINGS.items():
        icon = "✅" if texts.toggle_is_on(settings, key) else "❌"
        rows.append([_btn(f"{icon} {label}", f"cfg:{key}")])
    rows.append([_btn(texts.BTN_USERBOTS, "menu:userbots")])
    rows.append([_btn(texts.BTN_HYPER, "hyp:list")])
    rows.append([_btn(texts.BTN_MAIN_MENU, "menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_setting_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(texts.BTN_CANCEL, "menu:settings")]])


# ── Userbot accounts ───────────────────────────────────────────────────────────

def kb_userbot_list(userbots: list, page: int = 0) -> InlineKeyboardMarkup:
    page_ubs, total_pages = _paged(userbots, page)
    rows = []
    for ub in page_ubs:
        icon = texts.USERBOT_STATUS_LABELS.get(ub.status, "•").split()[0]
        star = "⭐ " if ub.is_default else ""
        rows.append([_btn(f"{icon} {star}{ub.display()[:40]}", f"ub:{ub.id}:view")])
    if total_pages > 1:
        rows.append(_nav_row("userbots", page, total_pages))
    rows.append([_btn(texts.BTN_ADD_USERBOT, "ub:new")])
    rows.append([_btn(texts.BTN_BACK, "menu:settings"), _btn(texts.BTN_MAIN_MENU, "menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_userbot_detail(ub) -> InlineKeyboardMarkup:
    rows = []
    if ub.status == "active":
        rows.append([_btn(texts.BTN_DISABLE_USERBOT, f"ub:{ub.id}:disable")])
    else:
        rows.append([_btn(texts.BTN_ENABLE_USERBOT, f"ub:{ub.id}:enable")])
    rows.append([_btn(texts.BTN_HYPER, f"hyp:{ub.id}:menu")])
    if not ub.is_default:
        rows.append([_btn(texts.BTN_REMOVE_USERBOT, f"ub:{ub.id}:confirm_remove")])
    rows.append([
        _btn(texts.BTN_REFRESH, f"ub:{ub.id}:view"),
        _btn(texts.BTN_BACK, "menu:userbots"),
    ])
    return InlineKeyboardMarkup(rows)


def kb_confirm_remove_userbot(userbot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn(texts.BTN_YES_REMOVE, f"ub:{userbot_id}:remove"),
        _btn(texts.BTN_CANCEL, f"ub:{userbot_id}:view"),
    ]])


def kb_userbot_cancel() -> InlineKeyboardMarkup:
    """Cancel button for every step of the add-account flow."""
    return InlineKeyboardMarkup([[_btn(texts.BTN_CANCEL, "ub:cancel_login")]])


# ── Hyper backup ───────────────────────────────────────────────────────────────

def kb_hyper_account_list(userbots: list, statuses: dict) -> InlineKeyboardMarkup:
    rows = []
    for ub in userbots:
        icon = "🟢" if statuses.get(ub.id) else "🔴"
        rows.append([_btn(f"{icon} {ub.display()[:40]}", f"hyp:{ub.id}:menu")])
    rows.append([_btn(texts.BTN_BACK, "menu:settings"), _btn(texts.BTN_MAIN_MENU, "menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_hyper_menu(acc_id: int, cfg: dict | None, dst, filters: dict) -> InlineKeyboardMarkup:
    enabled = bool(cfg and cfg["enabled"])
    rows = [[_btn("🟢 כבה הייפר" if enabled else "🔴 הפעל הייפר", f"hyp:{acc_id}:toggle")]]
    dst_label = dst.display()[:30] if dst else "בחר ערוץ גיבוי"
    rows.append([_btn(f"📤 יעד: {dst_label}", f"hyp:{acc_id}:pickdst")])
    for mtype in texts.HYPER_TYPES:
        rows.append([_btn(texts.hyper_type_button(mtype, filters.get(mtype)), f"hyp:{acc_id}:type:{mtype}")])
    rows.append([_btn(texts.BTN_BACK, "hyp:list")])
    return InlineKeyboardMarkup(rows)


def kb_hyper_type(acc_id: int, mtype: str, rule: dict | None) -> InlineKeyboardMarkup:
    enabled = rule is None or rule.get("enabled", True)
    rows = [[_btn("❌ אל תגבה סוג זה" if enabled else "✅ גבה סוג זה", f"hyp:{acc_id}:ttog:{mtype}")]]
    if enabled:
        rows.append([
            _btn(f"גודל מינ׳: {texts.fmt_size((rule or {}).get('min_size'))}", f"hyp:{acc_id}:set:{mtype}:minsize"),
            _btn("🗑", f"hyp:{acc_id}:clr:{mtype}:minsize"),
        ])
        rows.append([
            _btn(f"גודל מקס׳: {texts.fmt_size((rule or {}).get('max_size'))}", f"hyp:{acc_id}:set:{mtype}:maxsize"),
            _btn("🗑", f"hyp:{acc_id}:clr:{mtype}:maxsize"),
        ])
        if mtype in texts.HYPER_TYPES_WITH_DURATION:
            rows.append([
                _btn(f"אורך מינ׳: {texts.fmt_duration((rule or {}).get('min_duration'))}", f"hyp:{acc_id}:set:{mtype}:mindur"),
                _btn("🗑", f"hyp:{acc_id}:clr:{mtype}:mindur"),
            ])
            rows.append([
                _btn(f"אורך מקס׳: {texts.fmt_duration((rule or {}).get('max_duration'))}", f"hyp:{acc_id}:set:{mtype}:maxdur"),
                _btn("🗑", f"hyp:{acc_id}:clr:{mtype}:maxdur"),
            ])
        combine = (rule or {}).get("combine", "and")
        rows.append([_btn("🔗 חיבור: וגם" if combine == "and" else "🔀 חיבור: או", f"hyp:{acc_id}:comb:{mtype}")])
    rows.append([_btn(texts.BTN_BACK, f"hyp:{acc_id}:menu")])
    return InlineKeyboardMarkup(rows)


def kb_hyper_dst_picker(acc_id: int, dests: list) -> InlineKeyboardMarkup:
    rows = []
    for d in dests:
        rows.append([_btn(f"📤 {d.display()[:40]}", f"hyp:{acc_id}:dst:{d.id}")])
    rows.append([_btn(texts.BTN_BACK, f"hyp:{acc_id}:menu")])
    return InlineKeyboardMarkup(rows)


def kb_hyper_value_cancel(acc_id: int, mtype: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(texts.BTN_CANCEL, f"hyp:{acc_id}:type:{mtype}")]])


# ── Generic error back button ──────────────────────────────────────────────────

def kb_error_back(target: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(texts.BTN_BACK, f"menu:{target}")]])
