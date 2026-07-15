"""Handles the /start command. Creates a fresh main control message."""
from __future__ import annotations

import logging
from telethon import TelegramClient

from app.repositories import state_repo
from app.bot import state as _state
from app.bot.handlers._common import update_main_message
from app.ui import renderer
from app.ui.keyboards import to_telethon

logger = logging.getLogger(__name__)


async def start_command(bot: TelegramClient, event) -> None:
    """Send a new main control message; delete the old one if possible."""
    uid = event.sender_id
    chat_id = event.chat_id

    # Clear any in-flight wizard state
    _state.clear_user_data(uid)

    old_msg_id_str = state_repo.get_setting("main_message_id")
    old_chat_id_str = state_repo.get_setting("main_chat_id")

    # Send the new main message first
    text, keyboard = renderer.render_main_menu()
    msg = await bot.send_message(
        chat_id,
        text,
        buttons=to_telethon(keyboard),
        parse_mode="html",
        link_preview=False,
    )

    # Store the new message coordinates
    state_repo.set_setting("main_chat_id", str(chat_id))
    state_repo.set_setting("main_message_id", str(msg.id))

    # Delete the old message only after the new one is successfully stored
    if old_msg_id_str and old_chat_id_str:
        try:
            await bot.delete_messages(int(old_chat_id_str), int(old_msg_id_str))
        except Exception:
            pass  # Already gone or not accessible

    # Mark that main menu is currently visible
    _state._bot_data["on_main_screen"] = True
    logger.info("Main control message created: chat=%d msg=%d", chat_id, msg.id)
