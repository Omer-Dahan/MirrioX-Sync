"""Shared helpers for all bot handlers — Telethon edition."""
from __future__ import annotations

import logging
from datetime import datetime
from telethon import TelegramClient
from telethon.errors import MessageNotModifiedError, MessageIdInvalidError, RPCError

from app.repositories import state_repo
from app.bot import state as _state

logger = logging.getLogger(__name__)

# Tracks the start time of the current connectivity gap (None = connected)
_disconnected_since: datetime | None = None


def forget_main_message() -> None:
    """Drop the stored main-message coordinates.

    Called when the panel turns out to be gone (deleted, or a stale id left
    behind by a duplicated /start). Without this the auto-refresh loop retries
    the same dead message id every 30s forever and the panel stays frozen.
    """
    state_repo.set_setting("main_message_id", "")
    state_repo.set_setting("main_chat_id", "")
    _state._bot_data["on_main_screen"] = False


async def update_main_message(
    bot: TelegramClient,
    text: str,
    buttons: list | None,
) -> bool:
    """Edit the stored main control message with new text + keyboard.

    Returns True when the panel is known to be showing the requested content
    (edited, or already identical). Returns False when the edit did not land,
    so callers can avoid treating a dead panel as the active one.
    """
    global _disconnected_since

    chat_id_str = state_repo.get_setting("main_chat_id")
    msg_id_str = state_repo.get_setting("main_message_id")

    if not chat_id_str or not msg_id_str:
        logger.warning("No main message stored yet — cannot update")
        return False

    try:
        await bot.edit_message(
            int(chat_id_str),
            int(msg_id_str),
            text,
            buttons=buttons,
            parse_mode="html",
            link_preview=False,
        )
        if _disconnected_since is not None:
            downtime_s = (datetime.utcnow() - _disconnected_since).total_seconds()
            logger.info("Bot reconnected successfully after %.0fs", downtime_s)
            _disconnected_since = None
        return True

    except MessageNotModifiedError:
        return True  # Already showing this content — not an error

    except (MessageIdInvalidError, ValueError) as e:
        # The panel is gone for good. Forget it instead of retrying forever;
        # the next /start creates a fresh one.
        logger.warning(
            "Main message %s in chat %s is no longer editable (%s) — forgetting it",
            msg_id_str, chat_id_str, e,
        )
        forget_main_message()
        return False

    except RPCError as e:
        if _disconnected_since is None:
            _disconnected_since = datetime.utcnow()
            logger.warning(
                "Bot lost connectivity at %s: %s",
                _disconnected_since.strftime("%H:%M:%S"), e,
            )
        else:
            downtime_s = (datetime.utcnow() - _disconnected_since).total_seconds()
            logger.warning("Bot still disconnected (%.0fs so far): %s", downtime_s, e)
        return False


async def answer_callback(event, text: str = "") -> None:
    """Silently acknowledge a callback query event."""
    try:
        await event.answer(text)
    except Exception:
        pass


async def delete_user_message(event) -> None:
    """Delete the incoming user message (used after text-input steps)."""
    try:
        await event.delete()
    except Exception:
        pass
