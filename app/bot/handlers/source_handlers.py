"""Handlers for source and destination management screens."""
from __future__ import annotations

import asyncio
import logging
from telethon import TelegramClient

from app.models import ValidationError
from app.repositories import source_repo
from app.services import validation_service
from app.ui import renderer, texts, keyboards
from app.ui.keyboards import to_telethon
from app.bot import state as _state
from app.bot.handlers._common import update_main_message, answer_callback, delete_user_message
from app.worker import worker_main as _worker

logger = logging.getLogger(__name__)


async def dispatch_sources(bot: TelegramClient, event, uid: int) -> None:
    await answer_callback(event)
    data: str = event.data.decode()

    if data == "menu:sources":
        text, kb = renderer.render_source_list()
        await update_main_message(bot, text, to_telethon(kb))
    elif data == "src:new":
        await _src_add_start(bot, uid)
    elif ":" in data:
        parts = data.split(":")
        if len(parts) >= 3 and parts[0] == "src":
            src_id = int(parts[1])
            action = parts[2]
            await _dispatch_src_action(bot, uid, src_id, action)


async def _dispatch_src_action(bot: TelegramClient, uid: int, src_id: int, action: str) -> None:
    if action == "view":
        text, kb = renderer.render_source_detail(src_id)
        await update_main_message(bot, text, to_telethon(kb))
    elif action == "refresh_info":
        src = source_repo.get_source_by_id(src_id)
        if src:
            source_repo.reset_source_for_refresh(src_id)
            _worker.signal_resolve_now()
            loading_text = f"{texts.TITLE_SOURCE_DETAIL}: <b>{texts.esc(src.name)}</b>\n\n⏳ מאחזר מידע…"
            await update_main_message(bot, loading_text, to_telethon(renderer.render_source_detail(src_id)[1]))
            for _ in range(12):
                await asyncio.sleep(1)
                updated = source_repo.get_source_by_id(src_id)
                if updated and updated.channel_type is not None:
                    break
            text, kb = renderer.render_source_detail(src_id)
        else:
            text, kb = renderer.render_error("מקור לא נמצא", "sources")
        await update_main_message(bot, text, to_telethon(kb))
    elif action == "confirm_delete":
        src = source_repo.get_source_by_id(src_id)
        if src:
            text = f"⚠️ <b>מחיקת מקור</b>\n\nהאם למחוק את <b>{src.name}</b>? פעולה זו אינה הפיכה."
            kb = keyboards.kb_confirm_delete_source(src_id)
        else:
            text, kb = renderer.render_error("מקור לא נמצא", "sources")
        await update_main_message(bot, text, to_telethon(kb))
    elif action == "delete":
        if source_repo.is_source_in_use(src_id):
            text, kb = renderer.render_error(
                "לא ניתן למחוק מקור שמשויך למשימות קיימות.\nמחק את המשימות הקשורות תחילה.",
                back_target="sources",
            )
        else:
            source_repo.delete_source(src_id)
            text, kb = renderer.render_source_list()
        await update_main_message(bot, text, to_telethon(kb))


async def _src_add_start(bot: TelegramClient, uid: int) -> None:
    _state.get_user_data(uid)["awaiting_input"] = "source_ref"
    text = f"{texts.TITLE_SOURCES}\n\nהזן @username, מזהה מספרי, או קישור t.me/:\n<i>השם יישאב אוטומטית מהערוץ</i>"
    await update_main_message(bot, text, to_telethon(keyboards.kb_source_cancel()))


# ── Destinations ───────────────────────────────────────────────────────────────

async def dispatch_destinations(bot: TelegramClient, event, uid: int) -> None:
    await answer_callback(event)
    data: str = event.data.decode()

    if data == "menu:destinations":
        text, kb = renderer.render_dest_list()
        await update_main_message(bot, text, to_telethon(kb))
    elif data == "dst:new":
        await _dst_add_start(bot, uid)
    elif ":" in data:
        parts = data.split(":")
        if len(parts) >= 3 and parts[0] == "dst":
            dst_id = int(parts[1])
            action = parts[2]
            await _dispatch_dst_action(bot, uid, dst_id, action)


async def _dispatch_dst_action(bot: TelegramClient, uid: int, dst_id: int, action: str) -> None:
    if action == "view":
        text, kb = renderer.render_dest_detail(dst_id)
        await update_main_message(bot, text, to_telethon(kb))
    elif action == "refresh_info":
        dst = source_repo.get_destination_by_id(dst_id)
        if dst:
            source_repo.reset_destination_for_refresh(dst_id)
            _worker.signal_resolve_now()
            loading_text = f"{texts.TITLE_DEST_DETAIL}: <b>{texts.esc(dst.name)}</b>\n\n⏳ מאחזר מידע…"
            await update_main_message(bot, loading_text, to_telethon(renderer.render_dest_detail(dst_id)[1]))
            for _ in range(12):
                await asyncio.sleep(1)
                updated = source_repo.get_destination_by_id(dst_id)
                if updated and updated.channel_type is not None:
                    break
            text, kb = renderer.render_dest_detail(dst_id)
        else:
            text, kb = renderer.render_error("יעד לא נמצא", "destinations")
        await update_main_message(bot, text, to_telethon(kb))
    elif action == "confirm_delete":
        dest = source_repo.get_destination_by_id(dst_id)
        if dest:
            text = f"⚠️ <b>מחיקת יעד</b>\n\nהאם למחוק את <b>{dest.name}</b>? פעולה זו אינה הפיכה."
            kb = keyboards.kb_confirm_delete_dest(dst_id)
        else:
            text, kb = renderer.render_error("יעד לא נמצא", "destinations")
        await update_main_message(bot, text, to_telethon(kb))
    elif action == "delete":
        if source_repo.is_destination_in_use(dst_id):
            text, kb = renderer.render_error(
                "לא ניתן למחוק יעד שמשויך למשימות קיימות.\nמחק את המשימות הקשורות תחילה.",
                back_target="destinations",
            )
        else:
            source_repo.delete_destination(dst_id)
            text, kb = renderer.render_dest_list()
        await update_main_message(bot, text, to_telethon(kb))


async def _dst_add_start(bot: TelegramClient, uid: int) -> None:
    _state.get_user_data(uid)["awaiting_input"] = "dest_ref"
    text = f"{texts.TITLE_DESTINATIONS}\n\nהזן @username, מזהה מספרי, או קישור t.me/:\n<i>השם יישאב אוטומטית מהערוץ</i>"
    await update_main_message(bot, text, to_telethon(keyboards.kb_dest_cancel()))


# ── Text input handlers ────────────────────────────────────────────────────────

async def handle_source_ref(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    raw = (event.message.text or "").strip()
    try:
        ref = validation_service.validate_channel_ref(raw)
        if source_repo.get_source_by_ref(ref) is not None:
            text = f"{texts.TITLE_SOURCES}\n\n⚠️ מקור זה כבר קיים ברשימה.\n\nהזן @username, מזהה מספרי, או קישור t.me/:"
            kb = keyboards.kb_source_cancel()
            await update_main_message(bot, text, to_telethon(kb))
        else:
            new_src = source_repo.add_source(ref, ref)
            _worker.signal_resolve_now()
            
            loading_text = f"{texts.TITLE_SOURCES}\n\n⏳ בודק גישה לערוץ מול השרת..."
            await update_main_message(bot, loading_text, to_telethon(keyboards.kb_source_cancel()))
            
            for _ in range(12):
                await asyncio.sleep(1)
                updated = source_repo.get_source_by_id(new_src.id)
                if updated and (updated.resolved_id is not None or updated.validation_error is not None):
                    break
                    
            updated = source_repo.get_source_by_id(new_src.id)
            if updated and updated.validation_error:
                error_msg = updated.validation_error.lower()
                if "could not find" in error_msg or "cannot find" in error_msg or "find any entity" in error_msg:
                    source_repo.delete_source(new_src.id)
                    text = (
                        f"{texts.TITLE_SOURCES}\n\n"
                        f"⚠️ <b>הערוץ לא נגיש לבוט</b>\n\n"
                        f"לא ניתן למצוא את הערוץ או שהבוט אינו חבר בו.\n"
                        f"אנא ספק <b>קישור הצטרפות</b> (t.me/+) או קישור ציבורי כדי לאפשר לבוט להצטרף:"
                    )
                    kb = keyboards.kb_source_cancel()
                    await update_main_message(bot, text, to_telethon(kb))
                    return

            _state.get_user_data(uid).pop("awaiting_input", None)
            text, kb = renderer.render_source_list()
            await update_main_message(bot, text, to_telethon(kb))
    except ValidationError as e:
        text = f"{texts.TITLE_SOURCES}\n\n⚠️ {e}\n\nהזן @username, מזהה מספרי, או קישור t.me/:"
        kb = keyboards.kb_source_cancel()
        await update_main_message(bot, text, to_telethon(kb))


async def handle_dest_ref(bot: TelegramClient, event, uid: int) -> None:
    await delete_user_message(event)
    raw = (event.message.text or "").strip()
    try:
        ref = validation_service.validate_channel_ref(raw)
        if source_repo.get_destination_by_ref(ref) is not None:
            text = f"{texts.TITLE_DESTINATIONS}\n\n⚠️ יעד זה כבר קיים ברשימה.\n\nהזן @username, מזהה מספרי, או קישור t.me/:"
            kb = keyboards.kb_dest_cancel()
            await update_main_message(bot, text, to_telethon(kb))
        else:
            new_dst = source_repo.add_destination(ref, ref)
            _worker.signal_resolve_now()
            
            loading_text = f"{texts.TITLE_DESTINATIONS}\n\n⏳ בודק גישה לערוץ מול השרת..."
            await update_main_message(bot, loading_text, to_telethon(keyboards.kb_dest_cancel()))
            
            for _ in range(12):
                await asyncio.sleep(1)
                updated = source_repo.get_destination_by_id(new_dst.id)
                if updated and (updated.resolved_id is not None or updated.validation_error is not None):
                    break
                    
            updated = source_repo.get_destination_by_id(new_dst.id)
            if updated and updated.validation_error:
                error_msg = updated.validation_error.lower()
                if "could not find" in error_msg or "cannot find" in error_msg or "find any entity" in error_msg:
                    source_repo.delete_destination(new_dst.id)
                    text = (
                        f"{texts.TITLE_DESTINATIONS}\n\n"
                        f"⚠️ <b>הערוץ לא נגיש לבוט</b>\n\n"
                        f"לא ניתן למצוא את הערוץ או שהבוט אינו חבר בו.\n"
                        f"אנא ספק <b>קישור הצטרפות</b> (t.me/+) או קישור ציבורי כדי לאפשר לבוט להצטרף:"
                    )
                    kb = keyboards.kb_dest_cancel()
                    await update_main_message(bot, text, to_telethon(kb))
                    return

            _state.get_user_data(uid).pop("awaiting_input", None)
            text, kb = renderer.render_dest_list()
            await update_main_message(bot, text, to_telethon(kb))
    except ValidationError as e:
        text = f"{texts.TITLE_DESTINATIONS}\n\n⚠️ {e}\n\nהזן @username, מזהה מספרי, או קישור t.me/:"
        kb = keyboards.kb_dest_cancel()
        await update_main_message(bot, text, to_telethon(kb))
