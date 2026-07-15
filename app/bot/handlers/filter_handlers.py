"""Handlers for blocked words management."""
from __future__ import annotations

import logging
from telethon import TelegramClient

from app.models import ValidationError
from app.repositories import filter_repo
from app.services import validation_service
from app.ui import renderer, texts, keyboards
from app.ui.keyboards import to_telethon
from app.bot import state as _state
from app.bot.handlers._common import update_main_message, answer_callback, delete_user_message

logger = logging.getLogger(__name__)


async def dispatch(bot: TelegramClient, event, uid: int) -> None:
    await answer_callback(event)
    data: str = event.data.decode()

    if data == "menu:filters":
        text, kb = renderer.render_blocked_words()
        await update_main_message(bot, text, to_telethon(kb))
    elif data == "flt:new":
        await _add_word_start(bot, event, uid)
    elif data == "flt:confirm_clear":
        await update_main_message(
            bot, texts.CONFIRM_CLEAR_WORDS, to_telethon(keyboards.kb_confirm_clear_words())
        )
    elif data == "flt:clear":
        count = filter_repo.clear_all()
        logger.info("Cleared %d blocked words", count)
        text, kb = renderer.render_blocked_words()
        await update_main_message(bot, text, to_telethon(kb))
    elif data.startswith("flt:") and ":delete" in data:
        parts = data.split(":")
        word_id = int(parts[1])
        filter_repo.remove_by_id(word_id)
        text, kb = renderer.render_blocked_words()
        await update_main_message(bot, text, to_telethon(kb))


async def _add_word_start(bot: TelegramClient, event, uid: int) -> None:
    _state.get_user_data(uid)["awaiting_input"] = "filter_word"
    await update_main_message(
        bot, texts.PROMPT_BLOCKED_WORD, to_telethon(keyboards.kb_filter_cancel())
    )


async def handle_filter_word(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    raw = (event.message.text or "").strip()
    try:
        word = validation_service.validate_word(raw)
        filter_repo.add_word(word, added_by=uid)
        _state.get_user_data(uid).pop("awaiting_input", None)
        text, kb = renderer.render_blocked_words()
    except ValidationError as e:
        text = f"{texts.TITLE_BLOCKED_WORDS}\n\n⚠️ {e}\n\nהזן מילה לחסימה:"
        kb = keyboards.kb_filter_cancel()
    await update_main_message(bot, text, to_telethon(kb))
