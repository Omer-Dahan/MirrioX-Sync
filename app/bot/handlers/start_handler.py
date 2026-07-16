"""Handles the /start command. Shows the main control message."""
from __future__ import annotations

import logging
from telethon import TelegramClient
from telethon.errors import MessageNotModifiedError

from app.repositories import state_repo
from app.bot import state as _state
from app.bot.handlers._common import update_main_message
from app.ui import renderer
from app.ui.keyboards import to_telethon

logger = logging.getLogger(__name__)


async def start_command(bot: TelegramClient, event) -> None:
    """Show the main control message.

    If a panel already exists in this chat, edit it in place instead of
    deleting and recreating it — otherwise every /start (including ones fired
    by Telegram's menu/START button or reopening the chat) makes the panel
    blink out. The incoming /start command message is removed to keep the
    chat clean.
    """
    uid = event.sender_id
    chat_id = event.chat_id

    # Clear any in-flight wizard state
    _state.clear_user_data(uid)

    # Remove the user's /start command message so the chat does not fill up
    # with repeated "start" messages.
    try:
        await event.delete()
    except Exception:
        pass  # Already gone or not deletable

    old_msg_id_str = state_repo.get_setting("main_message_id")
    old_chat_id_str = state_repo.get_setting("main_chat_id")

    text, keyboard = renderer.render_main_menu()
    buttons = to_telethon(keyboard)

    # Reuse the existing panel if it lives in this chat — edit in place so the
    # panel never disappears.
    if old_msg_id_str and old_chat_id_str == str(chat_id):
        try:
            await bot.edit_message(
                int(chat_id),
                int(old_msg_id_str),
                text,
                buttons=buttons,
                parse_mode="html",
                link_preview=False,
            )
            _state._bot_data["on_main_screen"] = True
            logger.info("Main control message reused: chat=%d msg=%s", chat_id, old_msg_id_str)
            return
        except MessageNotModifiedError:
            # Panel already shows the menu — nothing to do.
            _state._bot_data["on_main_screen"] = True
            logger.info("Main control message already current: chat=%d msg=%s", chat_id, old_msg_id_str)
            return
        except Exception as e:
            # Message truly gone or not editable — fall through and create a new one.
            logger.info("Could not reuse main message (%s) — creating a fresh one", e)

    # Send a fresh main message
    msg = await bot.send_message(
        chat_id,
        text,
        buttons=buttons,
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
