"""Handlers for admin management and settings screens."""
from __future__ import annotations

import logging
from telethon import TelegramClient

from app.models import ValidationError
from app.repositories import admin_repo, state_repo
from app.services import validation_service
from app.ui import renderer, texts, keyboards
from app.ui.keyboards import to_telethon
from app.bot import state as _state
from app.bot.handlers._common import update_main_message, answer_callback, delete_user_message

logger = logging.getLogger(__name__)


async def dispatch_admins(bot: TelegramClient, event, uid: int) -> None:
    await answer_callback(event)
    data: str = event.data.decode()
    bootstrap_ids: list[int] = _state._bot_data.get("admin_ids", [])

    if data == "menu:admins":
        text, kb = renderer.render_admin_list(bootstrap_ids)
        await update_main_message(bot, text, to_telethon(kb))
    elif data == "adm:new":
        _state.get_user_data(uid)["awaiting_input"] = "admin_id"
        await update_main_message(bot, texts.PROMPT_ADMIN_ID, to_telethon(keyboards.kb_admin_cancel()))
    elif ":" in data:
        parts = data.split(":")
        if len(parts) >= 3 and parts[0] == "adm":
            tid = int(parts[1])
            action = parts[2]
            await _dispatch_admin_action(bot, uid, tid, action, bootstrap_ids)


async def _dispatch_admin_action(
    bot: TelegramClient, uid: int, telegram_id: int, action: str, bootstrap_ids: list[int]
) -> None:
    if action == "confirm_remove":
        if telegram_id in bootstrap_ids:
            text, kb = renderer.render_error("לא ניתן להסיר מנהל bootstrap", "admins")
        else:
            admin = admin_repo.get_by_telegram_id(telegram_id)
            name = f"@{admin.username}" if admin and admin.username else str(telegram_id)
            text = f"{texts.TITLE_ADMINS}\n\nהאם להסיר את {name} מרשימת המנהלים?"
            kb = keyboards.kb_confirm_remove_admin(telegram_id)
        await update_main_message(bot, text, to_telethon(kb))
    elif action == "remove":
        if telegram_id in bootstrap_ids:
            text, kb = renderer.render_error("לא ניתן להסיר מנהל bootstrap", "admins")
        else:
            admin_repo.remove(telegram_id)
            logger.info("Admin %d removed", telegram_id)
            text, kb = renderer.render_admin_list(bootstrap_ids)
        await update_main_message(bot, text, to_telethon(kb))


async def handle_admin_id(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    bootstrap_ids: list[int] = _state._bot_data.get("admin_ids", [])
    raw = (event.message.text or "").strip()
    try:
        tid = validation_service.validate_telegram_id(raw)
        admin_repo.add(tid, None, added_by=uid)
        logger.info("Admin %d added", tid)
        _state.get_user_data(uid).pop("awaiting_input", None)
        text, kb = renderer.render_admin_list(bootstrap_ids)
    except ValidationError as e:
        text = f"{texts.TITLE_ADMINS}\n\n⚠️ {e}\n\nהזן מזהה Telegram:"
        kb = keyboards.kb_admin_cancel()
    await update_main_message(bot, text, to_telethon(kb))


# ── Settings ───────────────────────────────────────────────────────────────────

async def dispatch_settings(bot: TelegramClient, event, uid: int) -> None:
    await answer_callback(event)
    data: str = event.data.decode()

    if data == "menu:settings":
        text, kb = renderer.render_settings()
        await update_main_message(bot, text, to_telethon(kb))
    elif data.startswith("cfg:"):
        key = data[4:]
        if key in texts.TOGGLE_SETTINGS:
            current = state_repo.get_setting(key) or "1"
            state_repo.set_setting(key, "0" if current == "1" else "1")
            text, kb = renderer.render_settings()
            await update_main_message(bot, text, to_telethon(kb))
        elif key in texts.EDITABLE_SETTINGS:
            ud = _state.get_user_data(uid)
            ud["awaiting_input"] = "setting_value"
            ud["setting_key"] = key
            await update_main_message(
                bot,
                texts.prompt_setting(key),
                to_telethon(keyboards.kb_setting_cancel()),
            )


async def handle_setting_value(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    ud = _state.get_user_data(uid)
    key = ud.pop("setting_key", None)
    raw = (event.message.text or "").strip()

    _limits = {
        "min_delay_ms":         (100,  60000),
        "max_delay_ms":         (100,  60000),
        "flood_wait_buffer_s":  (0,    300),
        "max_retries":          (1,    20),
        "heartbeat_interval_s": (5,    300),
    }

    if not key:
        text, kb = renderer.render_settings()
        await update_main_message(bot, text, to_telethon(kb))
        return

    label = texts.SETTINGS_LABELS.get(key, key)
    min_v, max_v = _limits.get(key, (0, 999999))

    try:
        val = validation_service.validate_positive_int_setting(raw, label, min_v, max_v)
        state_repo.set_setting(key, str(val))
        ud.pop("awaiting_input", None)
        text, kb = renderer.render_settings()
    except ValidationError as e:
        text = f"{texts.TITLE_SETTINGS}\n\n⚠️ {e}\n\n{texts.prompt_setting(key).rsplit(chr(10), maxsplit=1)[-1]}"
        kb = keyboards.kb_setting_cancel()
    await update_main_message(bot, text, to_telethon(kb))
