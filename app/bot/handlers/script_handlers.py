"""Global script library management (the `scr:` callback domain).

CRUD over reusable Python snippets. Running a script on a specific account is
handled in userbot_handlers (the account context lives there); this module only
manages the library itself: list, view, create (name → code), edit and delete.
"""
from __future__ import annotations

import logging

from telethon import TelegramClient

from app.repositories import script_repo
from app.ui import renderer, texts, keyboards
from app.ui.keyboards import to_telethon
from app.bot import state as _state
from app.bot.handlers._common import update_main_message, answer_callback, delete_user_message

logger = logging.getLogger(__name__)


async def dispatch(bot: TelegramClient, event, uid: int) -> None:
    """Route script-library callback queries."""
    await answer_callback(event)
    data: str = event.data.decode()

    if data == "scr:list":
        await _cancel_flow(uid)
        await _show_list(bot)
        return
    if data == "scr:new":
        await _start_new(bot, uid)
        return

    parts = data.split(":")
    if len(parts) >= 3 and parts[0] == "scr":
        try:
            script_id = int(parts[1])
        except ValueError:
            return
        action = parts[2]
        if action == "view":
            text, kb = renderer.render_script_detail(script_id)
        elif action == "edit":
            await _start_edit(bot, uid, script_id)
            return
        elif action == "confirm_delete":
            text, kb = renderer.render_script_confirm_delete(script_id)
        elif action == "delete":
            script_repo.delete_script(script_id)
            logger.info("Deleted script #%d", script_id)
            text, kb = renderer.render_scripts_list()
        else:
            return
        await update_main_message(bot, text, to_telethon(kb))


async def _show_list(bot: TelegramClient) -> None:
    text, kb = renderer.render_scripts_list()
    await update_main_message(bot, text, to_telethon(kb))


# ── Save flow (name → code) ─────────────────────────────────────────────────────

async def _start_new(bot: TelegramClient, uid: int) -> None:
    ud = _state.get_user_data(uid)
    ud.pop("script_edit_id", None)
    ud.pop("script_new_name", None)
    ud["awaiting_input"] = "script_name"
    await update_main_message(
        bot, texts.PROMPT_SCRIPT_NAME, to_telethon(keyboards.kb_script_cancel())
    )


async def _start_edit(bot: TelegramClient, uid: int, script_id: int) -> None:
    script = script_repo.get_script(script_id)
    if script is None:
        await _show_list(bot)
        return
    ud = _state.get_user_data(uid)
    ud.pop("script_new_name", None)
    ud["script_edit_id"] = script_id
    ud["awaiting_input"] = "script_code"
    await update_main_message(
        bot, texts.PROMPT_SCRIPT_CODE, to_telethon(keyboards.kb_script_cancel())
    )


async def _cancel_flow(uid: int) -> None:
    ud = _state.get_user_data(uid)
    ud.pop("awaiting_input", None)
    ud.pop("script_new_name", None)
    ud.pop("script_edit_id", None)


async def handle_script_name(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    name = (event.message.text or "").strip()
    ud = _state.get_user_data(uid)

    if not name:
        await update_main_message(
            bot,
            f"{texts.TITLE_SCRIPTS}\n\n⚠️ שם ריק. הזן שם לסקריפט:",
            to_telethon(keyboards.kb_script_cancel()),
        )
        return
    if script_repo.get_script_by_name(name) is not None:
        await update_main_message(
            bot,
            f"{texts.TITLE_SCRIPTS}\n\n⚠️ כבר קיים סקריפט בשם זה. הזן שם אחר:",
            to_telethon(keyboards.kb_script_cancel()),
        )
        return

    ud["script_new_name"] = name
    ud["awaiting_input"] = "script_code"
    await update_main_message(
        bot, texts.PROMPT_SCRIPT_CODE, to_telethon(keyboards.kb_script_cancel())
    )


async def handle_script_code(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    code = (event.message.text or "").strip()
    ud = _state.get_user_data(uid)
    edit_id = ud.get("script_edit_id")
    name = ud.get("script_new_name")

    if not code:
        await update_main_message(
            bot,
            f"{texts.TITLE_SCRIPTS}\n\n⚠️ קוד ריק. שלח את קוד הסקריפט:",
            to_telethon(keyboards.kb_script_cancel()),
        )
        return

    if edit_id is not None:
        script = script_repo.get_script(edit_id)
        if script is not None:
            script_repo.update_script(edit_id, script["name"], code)
            logger.info("Updated script #%d", edit_id)
        await _cancel_flow(uid)
        text, kb = renderer.render_script_detail(edit_id)
    elif name:
        script_id = script_repo.create_script(name, code)
        logger.info("Created script #%d '%s'", script_id, name)
        await _cancel_flow(uid)
        text, kb = renderer.render_script_detail(script_id)
    else:
        await _cancel_flow(uid)
        text, kb = renderer.render_scripts_list()

    await update_main_message(bot, text, to_telethon(kb))
