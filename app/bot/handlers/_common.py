"""Shared helpers for all bot handlers — Telethon edition."""
from __future__ import annotations

import logging
from datetime import datetime
from telethon import TelegramClient
from telethon.errors import (
    MessageNotModifiedError,
    MessageIdInvalidError,
    FloodWaitError,
    RPCError,
)

from app.repositories import state_repo
from app.bot import state as _state

logger = logging.getLogger(__name__)

# Tracks the start time of the current connectivity gap (None = connected)
_disconnected_since: datetime | None = None

# Monotonic-ish deadline: while Telegram is rate-limiting edits of the panel,
# skip further edits instead of queueing more requests behind the limit. Stored
# as a wall-clock datetime to keep this module free of a loop reference.
_edit_blocked_until: datetime | None = None


def edits_are_rate_limited() -> bool:
    """True while a FloodWait on the panel edit is still in effect."""
    return _edit_blocked_until is not None and datetime.utcnow() < _edit_blocked_until


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
    global _disconnected_since, _edit_blocked_until

    chat_id_str = state_repo.get_setting("main_chat_id")
    msg_id_str = state_repo.get_setting("main_message_id")

    if not chat_id_str or not msg_id_str:
        logger.warning("No main message stored yet — cannot update")
        return False

    # Still inside a FloodWait window — sending anyway only lengthens it.
    if edits_are_rate_limited():
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
        _edit_blocked_until = None  # edit landed — limit has cleared
        return True

    except MessageNotModifiedError:
        _edit_blocked_until = None
        return True  # Already showing this content — not an error

    # Must precede RPCError: FloodWaitError is a subclass of it, and this is a
    # rate limit, not a lost connection. With flood_sleep_threshold=0 on the bot
    # client this now surfaces instead of being slept through inside the call.
    except FloodWaitError as e:
        from datetime import timedelta
        # Floor of one refresh interval: Telegram sometimes reports a very short
        # (or zero) wait, and resuming edits immediately is what escalates a
        # brief limit into a long one.
        wait_s = max(int(getattr(e, "seconds", 0) or 0), 30)
        _edit_blocked_until = datetime.utcnow() + timedelta(seconds=wait_s)
        logger.warning(
            "Panel edits rate-limited by Telegram for %ds — pausing refreshes until then",
            wait_s,
        )
        return False

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
    """Acknowledge a callback query event; never raises.

    Acknowledging is best-effort by nature — an expired query cannot be answered
    and there is nothing to recover. A FloodWait here is worth a line, though: it
    means the acknowledgements themselves are being throttled, which is what
    makes buttons feel dead.
    """
    try:
        await event.answer(text)
    except FloodWaitError as e:
        logger.warning(
            "Callback acknowledgement rate-limited for %ds", getattr(e, "seconds", 0) or 0
        )
    except Exception:
        pass


async def delete_user_message(event) -> None:
    """Delete the incoming user message (used after text-input steps)."""
    try:
        await event.delete()
    except Exception:
        pass
