"""Job lifecycle and creation wizard handlers."""
from __future__ import annotations

import logging
from telethon import TelegramClient

from app.models import ALL_CONTENT_TYPES, DEFAULT_CONTENT_TYPES, JobError, ValidationError
from app.repositories import source_repo, filter_repo
from app.services import job_service, validation_service
from app.ui import renderer, texts, keyboards
from app.ui.keyboards import to_telethon
from app.bot import state as _state
from app.bot.handlers._common import update_main_message, answer_callback, delete_user_message

logger = logging.getLogger(__name__)


async def dispatch(bot: TelegramClient, event, uid: int) -> None:
    """Route job-related callback queries."""
    await answer_callback(event)
    data: str = event.data.decode()

    if data.startswith("je:"):
        await _dispatch_edit(bot, data)
        return

    if data == "menu:jobs":
        await _show_job_list(bot, uid)
    elif data == "job:new":
        await _wizard_start(bot, uid)
    elif data == "job:cancel_wizard":
        await _wizard_cancel(bot, uid)
    elif data == "wzd:skip_name":
        await _wizard_skip_name(bot, uid)
    elif data == "wzd:toggle_filter":
        await _wizard_toggle_filter(bot, uid)
    elif data == "wzd:toggle_group":
        await _wizard_toggle_group(bot, uid)
    elif data == "wzd:toggle_copy_text":
        await _wizard_toggle_copy_text(bot, uid)
    elif data == "wzd:toggle_continuous":
        await _wizard_toggle_continuous(bot, uid)
    elif data == "wzd:accounts":
        await _wizard_show_accounts(bot, uid)
    elif data == "wzd:all_ubs":
        await _wizard_all_accounts(bot, uid)
    elif data == "wzd:done_accounts":
        await _wizard_show_summary_from(bot, uid)
    elif data.startswith("wzd:toggle_ub:"):
        await _wizard_toggle_userbot(bot, uid, int(data.split(":")[2]))
    elif data == "wzd:confirm":
        await _wizard_confirm(bot, uid)
    elif data.startswith("wzd:toggle_src:"):
        await _wizard_toggle_source(bot, uid, int(data.split(":")[2]))
    elif data == "wzd:done_sources":
        await _wizard_done_sources(bot, uid)
    elif data.startswith("wzd:dst:"):
        await _wizard_toggle_dest(bot, uid, int(data.split(":")[2]))
    elif data == "wzd:done_dests":
        await _wizard_done_dests(bot, uid)
    elif data.startswith("wzd:mode:"):
        await _wizard_pick_mode(bot, uid, data.split(":")[2])
    elif data.startswith("wzd:toggle_type:"):
        await _wizard_toggle_type(bot, uid, data.split(":")[2])
    elif data == "wzd:done_types":
        await _wizard_done_types(bot, uid)
    elif data == "wzd:add_source":
        await _wizard_redirect_add_source(bot, uid)
    elif data == "wzd:add_dest":
        await _wizard_redirect_add_dest(bot, uid)
    elif ":" in data:
        parts = data.split(":")
        if len(parts) >= 3 and parts[0] == "job":
            job_id = int(parts[1])
            action = parts[2]
            await _dispatch_job_action(bot, uid, job_id, action)


async def _dispatch_job_action(bot: TelegramClient, uid: int, job_id: int, action: str) -> None:
    if action == "view":
        text, kb = renderer.render_job_detail(job_id)
        await update_main_message(bot, text, to_telethon(kb))
    elif action == "submit":
        await _job_submit(bot, job_id)
    elif action == "confirm_delete":
        await _job_confirm_delete(bot, job_id)
    elif action == "delete":
        await _job_delete(bot, uid, job_id)
    elif action == "confirm_cancel":
        await _job_confirm_cancel(bot, job_id)
    elif action == "cancel":
        await _job_cancel(bot, job_id)
    elif action == "pause":
        await _job_pause(bot, job_id)
    elif action == "resume":
        await _job_resume(bot, job_id)


# ── Job edit (draft / paused) ────────────────────────────────────────────────

async def _dispatch_edit(bot: TelegramClient, data: str) -> None:
    """Route je:<job_id>:<action>[:<param>] callbacks for editing a job's soft settings."""
    from app.repositories import job_repo, userbot_repo

    parts = data.split(":")
    if len(parts) < 3:
        return
    try:
        job_id = int(parts[1])
    except ValueError:
        return
    action = parts[2]
    param = parts[3] if len(parts) > 3 else None

    job = job_repo.get_by_id(job_id)
    if job is None:
        text, kb = renderer.render_error("משימה לא נמצאה", "jobs")
        await update_main_message(bot, text, to_telethon(kb))
        return
    # Editing is only safe before a job is live: a draft, or a job the user paused.
    if job.status not in ("draft", "paused"):
        text, kb = renderer.render_error(
            "ניתן לערוך רק משימות בטיוטה או מושהות. השהה את המשימה תחילה.", "jobs"
        )
        await update_main_message(bot, text, to_telethon(kb))
        return

    if action == "menu":
        await _edit_show_menu(bot, job_id)
    elif action == "tgl_filter":
        job_repo.update_flags(job_id, use_blocked_words=not job.use_blocked_words)
        await _edit_show_menu(bot, job_id)
    elif action == "tgl_group":
        job_repo.update_flags(job_id, group_media=not job.group_media)
        await _edit_show_menu(bot, job_id)
    elif action == "tgl_text":
        job_repo.update_flags(job_id, copy_text=not job.copy_text)
        await _edit_show_menu(bot, job_id)
    elif action == "tgl_cont":
        job_repo.update_flags(job_id, continuous=not job.continuous)
        await _edit_show_menu(bot, job_id)
    elif action == "reset_excl":
        job_repo.reset_exclusions(job_id)
        logger.info("Job #%d: access exclusions reset by user via edit", job_id)
        await _edit_show_menu(bot, job_id)
    elif action == "accounts":
        await _edit_show_accounts(bot, job_id)
    elif action == "all_ubs":
        job_repo.set_allowed_userbots(job_id, None)
        await _edit_show_accounts(bot, job_id)
    elif action == "ub" and param is not None:
        await _edit_toggle_userbot(bot, job, int(param))
    elif action == "types":
        await _edit_show_content_types(bot, job_id)
    elif action == "type" and param is not None:
        await _edit_toggle_type(bot, job, param)
    elif action == "dests":
        await _edit_show_destinations(bot, job_id)
    elif action == "dst" and param is not None:
        await _edit_toggle_destination(bot, job, int(param))


async def _edit_show_menu(bot: TelegramClient, job_id: int) -> None:
    text, kb = renderer.render_job_edit(job_id)
    await update_main_message(bot, text, to_telethon(kb))


async def _edit_show_accounts(bot: TelegramClient, job_id: int) -> None:
    text, kb = renderer.render_job_edit_accounts(job_id)
    await update_main_message(bot, text, to_telethon(kb))


async def _edit_show_content_types(bot: TelegramClient, job_id: int) -> None:
    text, kb = renderer.render_job_edit_content_types(job_id)
    await update_main_message(bot, text, to_telethon(kb))


async def _edit_toggle_userbot(bot: TelegramClient, job, userbot_id: int) -> None:
    from app.repositories import job_repo, userbot_repo

    active_ids = {u.id for u in userbot_repo.get_active()}
    # An empty allow-list means "all accounts", so start from the full active set.
    current = job.allowed_ids() or set(active_ids)
    if userbot_id in current:
        current.discard(userbot_id)
    else:
        current.add(userbot_id)
    # A job with no allowed account could never run — ignore the toggle that empties it.
    if current:
        # All active accounts selected (and nothing stale) collapses back to "no limit".
        value = None if current == active_ids else ",".join(str(i) for i in sorted(current))
        job_repo.set_allowed_userbots(job.id, value)
    await _edit_show_accounts(bot, job.id)


async def _edit_show_destinations(bot: TelegramClient, job_id: int) -> None:
    text, kb = renderer.render_job_edit_destinations(job_id)
    await update_main_message(bot, text, to_telethon(kb))


async def _edit_toggle_destination(bot: TelegramClient, job, dest_id: int) -> None:
    current = job.destination_id_list()
    if dest_id in current:
        current = [d for d in current if d != dest_id]
    else:
        current = current + [dest_id]
    # A job with no destination could never run — ignore the toggle that empties it.
    if current:
        try:
            job_service.update_destinations(job.id, current)
        except JobError as e:
            text, kb = renderer.render_error(str(e), "jobs")
            await update_main_message(bot, text, to_telethon(kb))
            return
    await _edit_show_destinations(bot, job.id)


async def _edit_toggle_type(bot: TelegramClient, job, type_name: str) -> None:
    from app.repositories import job_repo

    selected = {p.strip() for p in (job.content_types or DEFAULT_CONTENT_TYPES).split(",") if p.strip()}
    if type_name in selected:
        selected.discard(type_name)
    else:
        selected.add(type_name)
    # At least one content type must remain selected.
    if selected:
        job_repo.set_content_types(job.id, ",".join(sorted(selected)))
    await _edit_show_content_types(bot, job.id)


# ── Job list ───────────────────────────────────────────────────────────────────

async def _show_job_list(bot: TelegramClient, uid: int) -> None:
    text, kb = renderer.render_job_list(telegram_id=uid)
    await update_main_message(bot, text, to_telethon(kb))


# ── Job actions ────────────────────────────────────────────────────────────────

async def _job_submit(bot: TelegramClient, job_id: int) -> None:
    try:
        job_service.submit_job(job_id)
        text, kb = renderer.render_job_detail(job_id)
    except JobError as e:
        text, kb = renderer.render_error(str(e), back_target="jobs")
    await update_main_message(bot, text, to_telethon(kb))


async def _job_confirm_delete(bot: TelegramClient, job_id: int) -> None:
    from app.repositories import job_repo
    job = job_repo.get_by_id(job_id)
    if job is None:
        text, kb = renderer.render_error("משימה לא נמצאה", "jobs")
    else:
        text, kb = renderer.render_job_confirm_delete(job)
    await update_main_message(bot, text, to_telethon(kb))


async def _job_delete(bot: TelegramClient, uid: int, job_id: int) -> None:
    try:
        job_service.delete_job(job_id)
        text, kb = renderer.render_job_list(telegram_id=uid)
    except JobError as e:
        text, kb = renderer.render_error(str(e), "jobs")
    await update_main_message(bot, text, to_telethon(kb))


async def _job_confirm_cancel(bot: TelegramClient, job_id: int) -> None:
    from app.repositories import job_repo
    job = job_repo.get_by_id(job_id)
    if job is None:
        text, kb = renderer.render_error("משימה לא נמצאה", "jobs")
    else:
        text, kb = renderer.render_job_confirm_cancel(job)
    await update_main_message(bot, text, to_telethon(kb))


async def _job_cancel(bot: TelegramClient, job_id: int) -> None:
    try:
        job_service.cancel_job(job_id)
        text, kb = renderer.render_job_detail(job_id)
    except JobError as e:
        text, kb = renderer.render_error(str(e), "jobs")
    await update_main_message(bot, text, to_telethon(kb))


async def _job_pause(bot: TelegramClient, job_id: int) -> None:
    from app.repositories import job_repo
    job = job_repo.get_by_id(job_id)
    if job and job.status in ("pending", "running", "waiting_retry"):
        job_repo.pause_job(job_id)
        logger.info("Job #%d paused by user", job_id)
    text, kb = renderer.render_job_detail(job_id)
    await update_main_message(bot, text, to_telethon(kb))


async def _job_resume(bot: TelegramClient, job_id: int) -> None:
    from app.repositories import job_repo
    job = job_repo.get_by_id(job_id)
    if job and job.status == "paused":
        job_repo.resume_job(job_id)
        logger.info("Job #%d resumed by user", job_id)
    text, kb = renderer.render_job_detail(job_id)
    await update_main_message(bot, text, to_telethon(kb))


# ── Creation wizard ────────────────────────────────────────────────────────────

def _init_wizard(uid: int) -> dict:
    ud = _state.get_user_data(uid)
    ud["wizard"] = {
        "_step": 1,
        "_total": 7,
        "name": None,
        "source_ids": [],
        "source_names": [],
        "dest_ids": [],
        "dest_names": [],
        # Joined display string of all chosen destinations (kept for the summary texts).
        "dest_name": None,
        "mode": None,
        "date_from": None,
        "date_to": None,
        "id_from": None,
        "id_to": None,
        "single_id": None,
        "use_blocked_words": True,
        "group_media": True,
        "copy_text": True,
        "continuous": False,
        "content_types": set(ALL_CONTENT_TYPES),
        # None = not customized (all accounts). A set = the chosen allow-list.
        "allowed_ubs": None,
    }
    return ud["wizard"]


def _get_wizard(uid: int) -> dict | None:
    return _state.get_user_data(uid).get("wizard")


async def _wizard_start(bot: TelegramClient, uid: int) -> None:
    w = _init_wizard(uid)
    w["_step"] = 1
    _state.get_user_data(uid)["awaiting_input"] = "job_name"
    text, kb = renderer.render_wizard_step(texts.WIZARD_ENTER_NAME, w, keyboards.kb_wizard_name_step())
    await update_main_message(bot, text, to_telethon(kb))


async def _wizard_skip_name(bot: TelegramClient, uid: int) -> None:
    w = _get_wizard(uid)
    if not w:
        return
    w["name"] = None
    _state.get_user_data(uid).pop("awaiting_input", None)
    w["_step"] = 2
    await _wizard_show_source_select(bot, w)


async def _wizard_cancel(bot: TelegramClient, uid: int) -> None:
    ud = _state.get_user_data(uid)
    ud.pop("wizard", None)
    ud.pop("awaiting_input", None)
    text, kb = renderer.render_main_menu()
    await update_main_message(bot, text, to_telethon(kb))


async def _wizard_toggle_filter(bot: TelegramClient, uid: int) -> None:
    w = _get_wizard(uid)
    if not w:
        return
    w["use_blocked_words"] = not w.get("use_blocked_words", True)
    await _wizard_show_summary(bot, w)


async def _wizard_toggle_group(bot: TelegramClient, uid: int) -> None:
    w = _get_wizard(uid)
    if not w:
        return
    w["group_media"] = not w.get("group_media", True)
    await _wizard_show_summary(bot, w)


async def _wizard_toggle_copy_text(bot: TelegramClient, uid: int) -> None:
    w = _get_wizard(uid)
    if not w:
        return
    w["copy_text"] = not w.get("copy_text", True)
    await _wizard_show_summary(bot, w)


async def _wizard_toggle_continuous(bot: TelegramClient, uid: int) -> None:
    w = _get_wizard(uid)
    if not w:
        return
    # Orthogonal to the mode: the mode still picks which history to copy first,
    # and continuous keeps the job listening once that copy is done.
    w["continuous"] = not w.get("continuous", False)
    await _wizard_show_summary(bot, w)


async def _wizard_redirect_add_source(bot: TelegramClient, uid: int) -> None:
    text = f"{texts.TITLE_SOURCES}\n\nהזן @username, מזהה מספרי, או קישור t.me/:\n<i>השם יישאב אוטומטית מהערוץ</i>"
    await update_main_message(bot, text, to_telethon(keyboards.kb_wizard_cancel()))
    _state.get_user_data(uid)["awaiting_input"] = "wzd_source_ref"


async def _wizard_redirect_add_dest(bot: TelegramClient, uid: int) -> None:
    text = f"{texts.TITLE_DESTINATIONS}\n\nהזן @username, מזהה מספרי, או קישור t.me/:\n<i>השם יישאב אוטומטית מהערוץ</i>"
    await update_main_message(bot, text, to_telethon(keyboards.kb_wizard_cancel()))
    _state.get_user_data(uid)["awaiting_input"] = "wzd_dest_ref"


async def _wizard_toggle_source(bot: TelegramClient, uid: int, source_id: int) -> None:
    w = _get_wizard(uid)
    if not w:
        return
    src = source_repo.get_source_by_id(source_id)
    if src is None:
        text, kb = renderer.render_error("מקור לא נמצא")
        await update_main_message(bot, text, to_telethon(kb))
        return
    ids: list = w.setdefault("source_ids", [])
    names: list = w.setdefault("source_names", [])
    if source_id in ids:
        idx = ids.index(source_id)
        ids.pop(idx)
        names.pop(idx)
    else:
        ids.append(source_id)
        names.append(src.display())
    await _wizard_show_source_select(bot, w)


async def _wizard_done_sources(bot: TelegramClient, uid: int) -> None:
    w = _get_wizard(uid)
    if not w or not w.get("source_ids"):
        return
    w["_step"] = 3
    await _wizard_show_dest_select(bot, w)


async def _wizard_toggle_dest(bot: TelegramClient, uid: int, dest_id: int) -> None:
    w = _get_wizard(uid)
    if not w:
        return
    dest = source_repo.get_destination_by_id(dest_id)
    if dest is None:
        text, kb = renderer.render_error("יעד לא נמצא")
        await update_main_message(bot, text, to_telethon(kb))
        return
    ids: list = w.setdefault("dest_ids", [])
    names: list = w.setdefault("dest_names", [])
    if dest_id in ids:
        idx = ids.index(dest_id)
        ids.pop(idx)
        names.pop(idx)
    else:
        ids.append(dest_id)
        names.append(dest.display())
    w["dest_name"] = ", ".join(names) or None
    await _wizard_show_dest_select(bot, w)


async def _wizard_done_dests(bot: TelegramClient, uid: int) -> None:
    w = _get_wizard(uid)
    if not w or not w.get("dest_ids"):
        return
    w["_step"] = 4
    await _wizard_show_mode_select(bot, w)


async def _wizard_pick_mode(bot: TelegramClient, uid: int, mode: str) -> None:
    w = _get_wizard(uid)
    if not w:
        return
    w["mode"] = mode
    w["_step"] = 5

    if mode == "all":
        w["_step"] = 6
        await _wizard_show_content_types(bot, w)
    elif mode == "date_range":
        _state.get_user_data(uid)["awaiting_input"] = "job_date_from"
        text, kb = renderer.render_wizard_step(texts.WIZARD_ENTER_DATE_FROM, w, keyboards.kb_wizard_cancel())
        await update_main_message(bot, text, to_telethon(kb))
    elif mode == "id_range":
        _state.get_user_data(uid)["awaiting_input"] = "job_id_from"
        text, kb = renderer.render_wizard_step(texts.WIZARD_ENTER_ID_FROM, w, keyboards.kb_wizard_cancel())
        await update_main_message(bot, text, to_telethon(kb))
    elif mode == "single_id":
        _state.get_user_data(uid)["awaiting_input"] = "job_single_id"
        text, kb = renderer.render_wizard_step(texts.WIZARD_ENTER_SINGLE_ID, w, keyboards.kb_wizard_cancel())
        await update_main_message(bot, text, to_telethon(kb))


async def _wizard_show_source_select(bot: TelegramClient, w: dict) -> None:
    sources = source_repo.get_all_sources()
    selected = w.get("source_ids", [])
    if not sources:
        text, kb = renderer.render_wizard_step(texts.NO_SOURCES_YET, w, keyboards.kb_wizard_source_list([], selected))
    else:
        text, kb = renderer.render_wizard_step(texts.WIZARD_SELECT_SOURCE, w, keyboards.kb_wizard_source_list(sources, selected))
    await update_main_message(bot, text, to_telethon(kb))


async def _wizard_show_dest_select(bot: TelegramClient, w: dict) -> None:
    dests = source_repo.get_all_destinations()
    selected = w.get("dest_ids", [])
    if not dests:
        text, kb = renderer.render_wizard_step(texts.NO_DESTINATIONS_YET, w, keyboards.kb_wizard_dest_list([], selected))
    else:
        text, kb = renderer.render_wizard_step(texts.WIZARD_SELECT_DEST, w, keyboards.kb_wizard_dest_list(dests, selected))
    await update_main_message(bot, text, to_telethon(kb))


async def _wizard_show_mode_select(bot: TelegramClient, w: dict) -> None:
    text, kb = renderer.render_wizard_step(texts.WIZARD_SELECT_MODE, w, keyboards.kb_wizard_mode())
    await update_main_message(bot, text, to_telethon(kb))


async def _wizard_show_content_types(bot: TelegramClient, w: dict) -> None:
    selected: set = w.setdefault("content_types", set(ALL_CONTENT_TYPES))
    text, kb = renderer.render_wizard_step(texts.WIZARD_SELECT_CONTENT_TYPES, w, keyboards.kb_wizard_content_types(selected))
    await update_main_message(bot, text, to_telethon(kb))


async def _wizard_toggle_type(bot: TelegramClient, uid: int, type_name: str) -> None:
    w = _get_wizard(uid)
    if not w:
        return
    selected: set = w.setdefault("content_types", set(ALL_CONTENT_TYPES))
    if type_name in selected:
        selected.discard(type_name)
    else:
        selected.add(type_name)
    await _wizard_show_content_types(bot, w)


async def _wizard_done_types(bot: TelegramClient, uid: int) -> None:
    w = _get_wizard(uid)
    if not w or not w.get("content_types"):
        return
    w["_step"] = 7
    await _wizard_show_summary(bot, w)


async def _wizard_show_summary(bot: TelegramClient, w: dict) -> None:
    from app.repositories import userbot_repo

    word_count = filter_repo.count()
    text = texts.wizard_summary_text(w, word_count)
    # The account picker is only meaningful with more than one active account.
    accounts_label = (
        texts.wizard_accounts_label(w) if userbot_repo.count_active() > 1 else None
    )
    kb = keyboards.kb_wizard_summary(
        w.get("use_blocked_words", True),
        w.get("group_media", True),
        w.get("copy_text", True),
        w.get("continuous", False),
        accounts_label=accounts_label,
    )
    await update_main_message(bot, text, to_telethon(kb))


async def _wizard_show_summary_from(bot: TelegramClient, uid: int) -> None:
    w = _get_wizard(uid)
    if w:
        await _wizard_show_summary(bot, w)


# ── Account allow-list selection ─────────────────────────────────────────────

async def _wizard_show_accounts(bot: TelegramClient, uid: int) -> None:
    from app.repositories import userbot_repo

    w = _get_wizard(uid)
    if not w:
        return
    active = userbot_repo.get_active()
    selected = w.get("allowed_ubs")
    if selected is None:
        # First visit: start from "all selected" so leaving it as-is means no limit.
        selected = {u.id for u in active}
        w["allowed_ubs"] = selected
    text, kb = renderer.render_wizard_step(
        texts.WIZARD_SELECT_ACCOUNTS, w, keyboards.kb_wizard_userbot_list(active, selected)
    )
    await update_main_message(bot, text, to_telethon(kb))


async def _wizard_toggle_userbot(bot: TelegramClient, uid: int, userbot_id: int) -> None:
    from app.repositories import userbot_repo

    w = _get_wizard(uid)
    if not w:
        return
    selected: set = w.get("allowed_ubs")
    if selected is None:
        selected = {u.id for u in userbot_repo.get_active()}
        w["allowed_ubs"] = selected
    if userbot_id in selected:
        selected.discard(userbot_id)
    else:
        selected.add(userbot_id)
    await _wizard_show_accounts(bot, uid)


async def _wizard_all_accounts(bot: TelegramClient, uid: int) -> None:
    """Reset to 'all accounts' (no restriction) and return to the summary."""
    w = _get_wizard(uid)
    if not w:
        return
    w["allowed_ubs"] = None
    await _wizard_show_summary(bot, w)


async def _wizard_confirm(bot: TelegramClient, uid: int) -> None:
    w = _get_wizard(uid)
    if not w:
        text, kb = renderer.render_main_menu()
        await update_main_message(bot, text, to_telethon(kb))
        return

    source_ids: list = w.get("source_ids", [])
    source_names: list = w.get("source_names", [])
    if not source_ids:
        text, kb = renderer.render_error("לא נבחר אף מקור", "jobs")
        await update_main_message(bot, text, to_telethon(kb))
        return
    dest_ids: list = w.get("dest_ids", [])
    if not dest_ids:
        text, kb = renderer.render_error("לא נבחר אף יעד", "jobs")
        await update_main_message(bot, text, to_telethon(kb))
        return

    name_base = w.get("name")
    dst_label = ", ".join(
        n.split("(")[0].strip() for n in w.get("dest_names", [])
    )[:40] or "יעד"

    # Resolve the account allow-list once for every job the wizard creates.
    # A selection covering all active accounts (or the untouched default) imposes
    # no restriction and is stored as NULL.
    from app.repositories import userbot_repo

    selected = w.get("allowed_ubs")
    active_ids = {u.id for u in userbot_repo.get_active()}
    if selected and not (set(selected) >= active_ids):
        allowed_str = ",".join(str(i) for i in sorted(selected))
    else:
        allowed_str = None

    try:
        created = []
        for sid, sname in zip(source_ids, source_names):
            src_label = sname.split("(")[0].strip()
            if not name_base:
                job_name = f"{src_label} > {dst_label}"[:80]
            elif len(source_ids) > 1:
                job_name = f"{name_base} — {src_label}"[:80]
            else:
                job_name = name_base

            ct_set: set = w.get("content_types", set(ALL_CONTENT_TYPES))
            content_types_str = ",".join(sorted(ct_set)) if ct_set else DEFAULT_CONTENT_TYPES

            job = job_service.create_draft_job(
                name=job_name,
                source_id=sid,
                destination_id=dest_ids[0],
                destination_ids=dest_ids,
                mode=w["mode"],
                date_from=w.get("date_from"),
                date_to=w.get("date_to"),
                id_from=w.get("id_from"),
                id_to=w.get("id_to"),
                single_message_id=w.get("single_id"),
                use_blocked_words=w.get("use_blocked_words", True),
                group_media=w.get("group_media", True),
                copy_text=w.get("copy_text", True),
                content_types=content_types_str,
                created_by=uid,
                continuous=w.get("continuous", False),
                allowed_userbot_ids=allowed_str,
            )
            created.append(job)

        ud = _state.get_user_data(uid)
        ud.pop("wizard", None)
        ud.pop("awaiting_input", None)
        if len(created) == 1:
            text, kb = renderer.render_job_detail(created[0].id)
        else:
            text, kb = renderer.render_job_list(telegram_id=uid)
    except (JobError, ValidationError) as e:
        text, kb = renderer.render_error(str(e), "jobs")

    await update_main_message(bot, text, to_telethon(kb))


# ── Text input handlers ────────────────────────────────────────────────────────

async def handle_job_name(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    w = _get_wizard(uid)
    if not w:
        return
    raw = (event.message.text or "").strip()
    try:
        name = validation_service.validate_job_name(raw)
    except ValidationError as e:
        text, kb = renderer.render_wizard_step(
            f"⚠️ {e}\n\n{texts.WIZARD_ENTER_NAME}", w, keyboards.kb_wizard_cancel()
        )
        await update_main_message(bot, text, to_telethon(kb))
        return
    w["name"] = name
    w["_step"] = 2
    _state.get_user_data(uid).pop("awaiting_input", None)
    await _wizard_show_source_select(bot, w)


async def handle_job_date_from(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    w = _get_wizard(uid)
    if not w:
        return
    raw = (event.message.text or "").strip()
    try:
        validation_service.parse_date(raw, "תאריך התחלה")
        w["date_from"] = raw
        _state.get_user_data(uid)["awaiting_input"] = "job_date_to"
        text, kb = renderer.render_wizard_step(texts.WIZARD_ENTER_DATE_TO, w, keyboards.kb_wizard_cancel())
    except ValidationError as e:
        text, kb = renderer.render_wizard_step(
            f"⚠️ {e}\n\n{texts.WIZARD_ENTER_DATE_FROM}", w, keyboards.kb_wizard_cancel()
        )
    await update_main_message(bot, text, to_telethon(kb))


async def handle_job_date_to(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    w = _get_wizard(uid)
    if not w:
        return
    raw = (event.message.text or "").strip()
    try:
        validation_service.validate_date_range(w.get("date_from", ""), raw)
        w["date_to"] = raw
        _state.get_user_data(uid).pop("awaiting_input", None)
        w["_step"] = 6
        await _wizard_show_content_types(bot, w)
    except ValidationError as e:
        text, kb = renderer.render_wizard_step(
            f"⚠️ {e}\n\n{texts.WIZARD_ENTER_DATE_TO}", w, keyboards.kb_wizard_cancel()
        )
        await update_main_message(bot, text, to_telethon(kb))


async def handle_job_id_from(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    w = _get_wizard(uid)
    if not w:
        return
    raw = (event.message.text or "").strip()
    try:
        val = validation_service.validate_single_id(raw)
        w["id_from"] = val
        _state.get_user_data(uid)["awaiting_input"] = "job_id_to"
        text, kb = renderer.render_wizard_step(texts.WIZARD_ENTER_ID_TO, w, keyboards.kb_wizard_cancel())
    except ValidationError as e:
        text, kb = renderer.render_wizard_step(
            f"⚠️ {e}\n\n{texts.WIZARD_ENTER_ID_FROM}", w, keyboards.kb_wizard_cancel()
        )
    await update_main_message(bot, text, to_telethon(kb))


async def handle_job_id_to(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    w = _get_wizard(uid)
    if not w:
        return
    raw = (event.message.text or "").strip()
    try:
        id_from = w.get("id_from", 0)
        id_to = validation_service.validate_single_id(raw)
        if id_from and id_to <= id_from:
            raise ValidationError("מזהה הסיום חייב להיות גדול ממזהה ההתחלה")
        w["id_to"] = id_to
        _state.get_user_data(uid).pop("awaiting_input", None)
        w["_step"] = 6
        await _wizard_show_content_types(bot, w)
    except ValidationError as e:
        text, kb = renderer.render_wizard_step(
            f"⚠️ {e}\n\n{texts.WIZARD_ENTER_ID_TO}", w, keyboards.kb_wizard_cancel()
        )
        await update_main_message(bot, text, to_telethon(kb))


async def handle_job_single_id(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    w = _get_wizard(uid)
    if not w:
        return
    raw = (event.message.text or "").strip()
    try:
        val = validation_service.validate_single_id(raw)
        w["single_id"] = val
        _state.get_user_data(uid).pop("awaiting_input", None)
        w["_step"] = 6
        await _wizard_show_content_types(bot, w)
    except ValidationError as e:
        text, kb = renderer.render_wizard_step(
            f"⚠️ {e}\n\n{texts.WIZARD_ENTER_SINGLE_ID}", w, keyboards.kb_wizard_cancel()
        )
        await update_main_message(bot, text, to_telethon(kb))


async def handle_wzd_source_ref(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    w = _get_wizard(uid)
    raw = (event.message.text or "").strip()
    try:
        ref = validation_service.validate_channel_ref(raw)
        src = source_repo.add_source(ref, ref)
        _state.get_user_data(uid).pop("awaiting_input", None)
        if w:
            ids: list = w.setdefault("source_ids", [])
            names: list = w.setdefault("source_names", [])
            if src.id not in ids:
                ids.append(src.id)
                names.append(src.display())
            w["_step"] = 2
            await _wizard_show_source_select(bot, w)
        else:
            text, kb = renderer.render_source_list()
            await update_main_message(bot, text, to_telethon(kb))
    except ValidationError as e:
        text = f"{texts.TITLE_SOURCES}\n\n⚠️ {e}\n\nהזן @username, מזהה מספרי, או קישור t.me/:"
        await update_main_message(bot, text, to_telethon(keyboards.kb_wizard_cancel()))


async def handle_wzd_dest_ref(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    w = _get_wizard(uid)
    raw = (event.message.text or "").strip()
    try:
        ref = validation_service.validate_channel_ref(raw)
        dest = source_repo.add_destination(ref, ref)
        _state.get_user_data(uid).pop("awaiting_input", None)
        if w:
            ids: list = w.setdefault("dest_ids", [])
            names: list = w.setdefault("dest_names", [])
            if dest.id not in ids:
                ids.append(dest.id)
                names.append(dest.display())
            w["dest_name"] = ", ".join(names) or None
            w["_step"] = 3
            await _wizard_show_dest_select(bot, w)
        else:
            text, kb = renderer.render_dest_list()
            await update_main_message(bot, text, to_telethon(kb))
    except ValidationError as e:
        text = f"{texts.TITLE_DESTINATIONS}\n\n⚠️ {e}\n\nהזן @username, מזהה מספרי, או קישור t.me/:"
        await update_main_message(bot, text, to_telethon(keyboards.kb_wizard_cancel()))
