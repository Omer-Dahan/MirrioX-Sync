"""Handlers for the duplicate-scan flow."""
from __future__ import annotations

import logging
from telethon import TelegramClient

from app.repositories import scan_repo, source_repo
from app.services import validation_service
from app.models import ValidationError
from app.ui import renderer, texts, keyboards
from app.ui.keyboards import to_telethon
from app.bot import state as _state
from app.bot.handlers._common import update_main_message, answer_callback, delete_user_message

logger = logging.getLogger(__name__)


async def dispatch_scan(bot: TelegramClient, event, uid: int) -> None:
    await answer_callback(event)
    data: str = event.data.decode()

    if data == "menu:scan":
        text, kb = renderer.render_scan_picker()
        await update_main_message(bot, text, to_telethon(kb))
        return

    if not data.startswith("scan:"):
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "manual":
        _state.get_user_data(uid)["awaiting_input"] = "scan_channel_ref"
        text = f"{texts.TITLE_SCAN_PICKER}\n\nהזן @username, מזהה מספרי, או קישור t.me/ של הערוץ לסריקה:"
        await update_main_message(bot, text, to_telethon(keyboards.kb_scan_cancel()))

    elif action == "dst" and len(parts) >= 3:
        dest_id = int(parts[2])
        dest = source_repo.get_destination_by_id(dest_id)
        if dest is None:
            text, kb = renderer.render_error("יעד לא נמצא", "scan")
            await update_main_message(bot, text, to_telethon(kb))
            return
        text, kb = renderer.render_scan_channel_menu(dest.channel_ref, dest.title or dest.name)
        await update_main_message(bot, text, to_telethon(kb))

    elif action == "menu_ref" and len(parts) >= 3:
        ref = parts[2]
        dest = source_repo.get_destination_by_ref(ref)
        title = dest.title or dest.name if dest else ref
        text, kb = renderer.render_scan_channel_menu(ref, title)
        await update_main_message(bot, text, to_telethon(kb))

    elif action == "hist" and len(parts) >= 4:
        ref = parts[2]
        page = int(parts[3])
        text, kb = renderer.render_scan_history(ref, page)
        await update_main_message(bot, text, to_telethon(kb))

    elif action == "new" and len(parts) >= 3:
        ref = parts[2]
        dest = source_repo.get_destination_by_ref(ref)
        title = dest.title or dest.name if dest else ref
        dest_id = dest.id if dest else None
        await _start_scan_for_channel(bot, event, uid, channel_ref=ref, channel_title=title, dest_id=dest_id)

    elif action == "view" and len(parts) >= 3:
        scan_id = int(parts[2])
        text, kb = renderer.render_scan_report_by_id(scan_id)
        await update_main_message(bot, text, to_telethon(kb))

    elif action == "confirm_delete" and len(parts) >= 5:
        # scan:confirm_delete:{scan_id}:{page}:{channel_ref}
        scan_id = int(parts[2])
        text, kb = renderer.render_confirm_delete_dupes_by_id(scan_id)
        
        # We need to maintain context for back button in history
        # Let's override the return button manually here
        page = int(parts[3])
        ref = parts[4]
        from telegram import InlineKeyboardMarkup
        from app.ui.keyboards import _btn, to_telethon as _to_telethon
        from app.ui import texts
        kb = InlineKeyboardMarkup([
            [_btn(texts.BTN_YES_DELETE, f"scan:delete:{scan_id}:{page}:{ref}")],
            [_btn(texts.BTN_CANCEL, f"scan:hist:{ref}:{page}"),]
        ])
        await update_main_message(bot, text, to_telethon(kb))

    elif action == "delete" and len(parts) >= 3:
        scan_id = int(parts[2])
        # Can have extra parts but _queue_delete handles only scan_id
        await _queue_delete(bot, event, uid, scan_id)

    elif action == "confirm_del_scan" and len(parts) >= 5:
        scan_id = int(parts[2])
        page = int(parts[3])
        ref = parts[4]
        from telegram import InlineKeyboardMarkup
        from app.ui.keyboards import _btn
        kb = InlineKeyboardMarkup([
            [_btn(texts.BTN_CONFIRM_DEL_SCAN, f"scan:del_scan:{scan_id}:{page}:{ref}")],
            [_btn(texts.BTN_CANCEL, f"scan:hist:{ref}:{page}")],
        ])
        await update_main_message(bot, texts.confirm_del_scan_text(), to_telethon(kb))

    elif action == "del_scan" and len(parts) >= 5:
        scan_id = int(parts[2])
        page = int(parts[3])
        ref = parts[4]
        scan_repo.cancel_scan(scan_id)
        scan_repo.delete_scan(scan_id)
        logger.info("Deleted scan #%d for channel %s", scan_id, ref)
        # Render history page again
        text, kb = renderer.render_scan_history(ref, page)
        await update_main_message(bot, text, to_telethon(kb))

    elif action == "stop_hist" and len(parts) >= 5:
        scan_id = int(parts[2])
        page = int(parts[3])
        ref = parts[4]
        scan_repo.cancel_scan(scan_id)
        text, kb = renderer.render_scan_history(ref, page)
        await update_main_message(bot, text, to_telethon(kb))

    elif action == "stop" and len(parts) >= 3:
        scan_id = int(parts[2])
        scan_repo.cancel_scan(scan_id)
        text, kb = renderer.render_scan_report_by_id(scan_id)
        await update_main_message(bot, text, to_telethon(kb))


async def handle_scan_channel_ref(bot: TelegramClient, event, uid: int) -> None:
    """Handle manually typed channel ref for scanning."""
    await delete_user_message(event)
    raw = (event.message.text or "").strip()
    try:
        ref = validation_service.validate_channel_ref(raw)
    except ValidationError as e:
        text = f"{texts.TITLE_SCAN_PICKER}\n\n⚠️ {e}\n\nהזן @username, מזהה מספרי, או קישור t.me/:"
        await update_main_message(bot, text, to_telethon(keyboards.kb_scan_cancel()))
        return

    _state.get_user_data(uid).pop("awaiting_input", None)
    dest = source_repo.get_destination_by_ref(ref)
    title = dest.title or dest.name if dest else ref
    text, kb = renderer.render_scan_channel_menu(ref, title)
    await update_main_message(bot, text, to_telethon(kb))


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _start_scan_for_channel(
    bot: TelegramClient, event, uid: int,
    channel_ref: str, channel_title: str, dest_id: int | None,
) -> None:
    existing = scan_repo.get_latest_scan_for_channel(channel_ref)
    if existing and existing["status"] in ("pending", "running"):
        text, kb = renderer.render_scan_history(channel_ref, 0)
        await update_main_message(bot, text, to_telethon(kb))
        return

    scan_repo.create_scan(channel_ref, channel_title, dest_id)
    text, kb = renderer.render_scan_history(channel_ref, 0)
    await update_main_message(bot, text, to_telethon(kb))


async def _queue_delete(bot: TelegramClient, event, uid: int, scan_id: int) -> None:
    scan = scan_repo.get_scan_by_id(scan_id)
    if scan is None or scan["status"] != "done":
        text, kb = renderer.render_error("לא נמצאה סריקה מושלמת", "scan")
        await update_main_message(bot, text, to_telethon(kb))
        return

    existing_del = scan_repo.get_latest_delete_job(scan_id)
    if existing_del and existing_del["status"] in ("pending", "running"):
        text = (
            f"{texts.TITLE_SCAN_REPORT} — <b>{texts.esc(scan['channel_title'])}</b>\n\n"
            "⏳ משימת המחיקה כבר בתהליך..."
        )
        kb = keyboards.kb_scan_report(scan_id, "done", True)
        await update_main_message(bot, text, to_telethon(kb))
        return

    scan_repo.create_delete_job(scan_id)
    text = (
        f"{texts.TITLE_SCAN_REPORT} — <b>{texts.esc(scan['channel_title'])}</b>\n\n"
        "🗑 משימת המחיקה הועברה לתור..."
    )
    kb = keyboards.kb_scan_report(scan_id, "done", True)
    await update_main_message(bot, text, to_telethon(kb))
