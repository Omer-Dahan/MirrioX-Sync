"""Userbot account management: list, enable/disable, remove, and interactive sign-in."""
from __future__ import annotations

import logging

from telethon import TelegramClient

from app.config import load_config
from app.models import ValidationError
from app.repositories import job_repo, userbot_repo, hyper_repo, script_repo, state_repo
from app.services import userbot_auth_service as auth
from app.ui import renderer, texts, keyboards
from app.ui.keyboards import to_telethon
from app.bot import state as _state
from app.bot.handlers._common import update_main_message, answer_callback, delete_user_message

logger = logging.getLogger(__name__)


async def dispatch(bot: TelegramClient, event, uid: int) -> None:
    """Route userbot-related callback queries."""
    await answer_callback(event)
    data: str = event.data.decode()

    if data == "menu:userbots":
        await _show_list(bot)
    elif data == "ub:new":
        await _start_add(bot, uid)
    elif data == "ub:cancel_login":
        await _cancel_add(bot, uid)
    elif ":" in data:
        parts = data.split(":")
        if len(parts) >= 3 and parts[0] == "ub":
            try:
                userbot_id = int(parts[1])
            except ValueError:
                return
            await _dispatch_action(bot, uid, userbot_id, parts)


async def _show_list(bot: TelegramClient) -> None:
    text, kb = renderer.render_userbot_list()
    await update_main_message(bot, text, to_telethon(kb))


async def _dispatch_action(
    bot: TelegramClient, uid: int, userbot_id: int, parts: list[str]
) -> None:
    action = parts[2]
    if action == "view":
        text, kb = renderer.render_userbot_detail(userbot_id)
    elif action == "enable":
        userbot_repo.set_status(userbot_id, "active", None)
        # This account may reach channels the others could not.
        job_repo.reset_all_exclusions()
        logger.info("Userbot #%d enabled", userbot_id)
        text, kb = renderer.render_userbot_detail(userbot_id)
    elif action == "disable":
        _release_jobs(userbot_id)
        userbot_repo.set_status(userbot_id, "inactive", None)
        failed = script_repo.fail_pending_tasks(userbot_id, "בוטל: החשבון הושבת לפני שהמשימה רצה")
        if failed:
            logger.info("Userbot #%d disabled — %d pending task(s) failed", userbot_id, failed)
        logger.info("Userbot #%d disabled", userbot_id)
        text, kb = renderer.render_userbot_detail(userbot_id)
    elif action == "confirm_remove":
        text, kb = renderer.render_userbot_confirm_remove(userbot_id)
    elif action == "remove":
        await _remove(userbot_id)
        text, kb = renderer.render_userbot_list()
    elif action == "runmenu":
        text, kb = renderer.render_userbot_run_menu(userbot_id)
    elif action == "runquick":
        await _start_run(bot, uid, userbot_id)
        return
    elif action == "scripts":
        text, kb = renderer.render_scripts_for_userbot(userbot_id)
    elif action == "runscript":
        if len(parts) < 4:
            return
        try:
            script_id = int(parts[3])
        except ValueError:
            return
        await _run_saved_script(bot, uid, userbot_id, script_id)
        return
    else:
        return
    await update_main_message(bot, text, to_telethon(kb))


# ── Ad-hoc code execution ───────────────────────────────────────────────────────

def _adhoc_enabled() -> bool:
    return state_repo.get_setting("adhoc_enabled") != "0"


async def _start_run(bot: TelegramClient, uid: int, userbot_id: int) -> None:
    """Prompt the admin for a Python snippet to run on this account."""
    if not _adhoc_enabled():
        text, kb = renderer.render_userbot_run_menu(userbot_id)
        await update_main_message(bot, text, to_telethon(kb))
        return
    ud = _state.get_user_data(uid)
    ud["awaiting_input"] = "userbot_run_code"
    ud["run_userbot_id"] = userbot_id
    await update_main_message(
        bot, texts.PROMPT_RUN_CODE, to_telethon(keyboards.kb_run_cancel(userbot_id))
    )


async def _run_saved_script(
    bot: TelegramClient, uid: int, userbot_id: int, script_id: int
) -> None:
    """Enqueue a saved script to run on this account."""
    script = script_repo.get_script(script_id)
    if script is None or not _adhoc_enabled():
        text, kb = renderer.render_scripts_for_userbot(userbot_id)
        await update_main_message(bot, text, to_telethon(kb))
        return
    script_repo.enqueue_task(userbot_id, script["code"], chat_id=uid, script_id=script_id)
    logger.info("Enqueued saved script #%d on userbot #%d", script_id, userbot_id)
    await update_main_message(
        bot, texts.RUN_SENT_TEXT, to_telethon(keyboards.kb_userbot_run_menu(userbot_id))
    )


async def handle_userbot_run_code(bot: TelegramClient, event, uid: int) -> None:
    """Capture the snippet text and enqueue it as an ad-hoc task."""
    await delete_user_message(event)
    ud = _state.get_user_data(uid)
    code = (event.message.text or "").strip()
    userbot_id = ud.pop("run_userbot_id", None)
    ud.pop("awaiting_input", None)

    if userbot_id is None:
        text, kb = renderer.render_userbot_list()
        await update_main_message(bot, text, to_telethon(kb))
        return
    if not code or not _adhoc_enabled():
        text, kb = renderer.render_userbot_run_menu(userbot_id)
        await update_main_message(bot, text, to_telethon(kb))
        return

    script_repo.enqueue_task(userbot_id, code, chat_id=uid)
    logger.info("Enqueued ad-hoc code on userbot #%d", userbot_id)
    await update_main_message(
        bot, texts.RUN_SENT_TEXT, to_telethon(keyboards.kb_userbot_run_menu(userbot_id))
    )


def _release_jobs(userbot_id: int) -> int:
    """
    Hand this account's listening jobs back so another account re-registers them.

    Jobs actively being copied are deliberately left alone — that includes a
    continuous job still in its backfill phase, which is an ordinary bulk run.
    The runner is inside run_job() and won't notice the account was disabled
    until that job ends; releasing it now would let a second account claim it
    and copy the same messages twice. It finishes first, then the runner exits.
    """
    released = 0
    running_bulk = 0
    for job in job_repo.get_all(status_filter=["running"]):
        if job.assigned_userbot_id != userbot_id:
            continue
        if job.continuous and job.backfill_done:
            # Safe to reassign immediately — the live handler ignores messages
            # once the job is no longer assigned to it.
            job_repo.release_job(job.id, "running")
            released += 1
        else:
            running_bulk += 1
    if released:
        logger.info("Released %d continuous job(s) from userbot #%d", released, userbot_id)
    if running_bulk:
        logger.info(
            "Userbot #%d still has %d bulk job(s) in flight — they will finish first",
            userbot_id, running_bulk,
        )
    return running_bulk


async def _remove(userbot_id: int) -> None:
    ub = userbot_repo.get_by_id(userbot_id)
    if ub is None or ub.is_default:
        return
    _release_jobs(userbot_id)
    script_repo.fail_pending_tasks(userbot_id, "בוטל: החשבון נמחק לפני שהמשימה רצה")
    hyper_repo.delete_config(userbot_id)
    userbot_repo.delete(userbot_id)
    # Best-effort: the worker's runner notices the row is gone and exits within a
    # poll cycle, so the session file may still be locked right now. A leftover
    # file is harmless — start_login() clears stale files before reusing a name.
    auth.remove_session_files(ub.session_name)
    logger.info("Userbot #%d (%s) removed", userbot_id, ub.display())


# ── Add-account flow ───────────────────────────────────────────────────────────

async def _start_add(bot: TelegramClient, uid: int) -> None:
    await auth.cancel(uid)
    _state.get_user_data(uid)["awaiting_input"] = "userbot_phone"
    await update_main_message(
        bot, texts.PROMPT_USERBOT_PHONE, to_telethon(keyboards.kb_userbot_cancel())
    )


async def _cancel_add(bot: TelegramClient, uid: int) -> None:
    await auth.cancel(uid)
    _state.get_user_data(uid).pop("awaiting_input", None)
    await _show_list(bot)


async def handle_userbot_phone(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    raw = (event.message.text or "").strip()
    try:
        await auth.start_login(uid, load_config(), raw)
    except ValidationError as e:
        text = f"{texts.TITLE_ADD_USERBOT}\n\n⚠️ {e}\n\nהזן מספר טלפון עם קידומת מדינה:"
        await update_main_message(bot, text, to_telethon(keyboards.kb_userbot_cancel()))
        return

    _state.get_user_data(uid)["awaiting_input"] = "userbot_code"
    await update_main_message(
        bot, texts.PROMPT_USERBOT_CODE, to_telethon(keyboards.kb_userbot_cancel())
    )


async def handle_userbot_code(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    raw = (event.message.text or "").strip()
    try:
        signed_in = await auth.submit_code(uid, raw)
    except ValidationError as e:
        pending = auth.get_pending(uid)
        if pending is None:
            # Login expired — restart from the phone step.
            _state.get_user_data(uid)["awaiting_input"] = "userbot_phone"
            text = f"{texts.TITLE_ADD_USERBOT}\n\n⚠️ {e}\n\nהזן מספר טלפון עם קידומת מדינה:"
        else:
            text = f"{texts.TITLE_ADD_USERBOT}\n\n⚠️ {e}\n\nהזן את הקוד שקיבלת בטלגרם:"
        await update_main_message(bot, text, to_telethon(keyboards.kb_userbot_cancel()))
        return

    if not signed_in:
        _state.get_user_data(uid)["awaiting_input"] = "userbot_2fa"
        await update_main_message(
            bot, texts.PROMPT_USERBOT_2FA, to_telethon(keyboards.kb_userbot_cancel())
        )
        return

    await _finish(bot, uid)


async def handle_userbot_2fa(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    raw = (event.message.text or "").strip()
    try:
        await auth.submit_password(uid, raw)
    except ValidationError as e:
        pending = auth.get_pending(uid)
        if pending is None:
            _state.get_user_data(uid)["awaiting_input"] = "userbot_phone"
            text = f"{texts.TITLE_ADD_USERBOT}\n\n⚠️ {e}\n\nהזן מספר טלפון עם קידומת מדינה:"
        else:
            text = f"{texts.TITLE_ADD_USERBOT}\n\n⚠️ {e}\n\nהזן את סיסמת ה-2FA:"
        await update_main_message(bot, text, to_telethon(keyboards.kb_userbot_cancel()))
        return

    await _finish(bot, uid)


async def _finish(bot: TelegramClient, uid: int) -> None:
    _state.get_user_data(uid).pop("awaiting_input", None)
    text, kb = renderer.render_userbot_list()
    await update_main_message(bot, text, to_telethon(kb))
