"""Management bot — Telethon MTProto edition.

Replaces python-telegram-bot (HTTP API, blocked by ISP) with Telethon bot mode
(MTProto, same protocol as the worker — works fine on all networks).
"""
from __future__ import annotations

import asyncio
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from app.config import Config
from app.repositories import admin_repo
from app.bot import state as _state
from app.bot.handlers._common import update_main_message
from app.ui import keyboards, renderer
from app.ui.keyboards import to_telethon

logger = logging.getLogger(__name__)

_AUTO_REFRESH_INTERVAL_S = 30

# Module-level bot reference — set once at startup.
# Used by the worker to send proactive notifications.
_bot: TelegramClient | None = None
_admin_chat_id: int | None = None  # chat to send notifications to


# ── Notification helper ────────────────────────────────────────────────────────

async def send_notification(chat_id: int, text: str) -> None:
    """Send a message via the management bot (MTProto)."""
    if _bot is None:
        logger.warning("send_notification called before bot is ready")
        return
    try:
        await _bot.send_message(chat_id, text, parse_mode="html")
    except Exception as e:
        logger.warning("send_notification failed: %s", e)


async def send_document(chat_id: int, file, caption=None, filename=None) -> None:
    """
    Send a document via the management bot. `file` may be a path (str) or raw bytes;
    for bytes, `filename` names the uploaded file. Used by the ad-hoc code feature
    to deliver a snippet's exported file back to the admin.
    """
    if _bot is None:
        logger.warning("send_document called before bot is ready")
        return
    try:
        upload = file
        if isinstance(file, (bytes, bytearray)):
            import io
            upload = io.BytesIO(bytes(file))
            upload.name = filename or "file.bin"
        # parse_mode=None: the caption is arbitrary user text and must not be parsed.
        await _bot.send_file(chat_id, upload, caption=caption, parse_mode=None)
    except Exception as e:
        logger.warning("send_document failed: %s", e)


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _is_authorized(uid: int, bootstrap_ids: list[int]) -> bool:
    return uid in bootstrap_ids or admin_repo.is_admin(uid)


def _get_uid(event) -> int:
    """Return the sender's user ID from any Telethon event."""
    return event.sender_id


# ── Auto-refresh ───────────────────────────────────────────────────────────────

async def _auto_refresh_loop(bot: TelegramClient) -> None:
    """Periodically refresh the main menu while the main screen is visible."""
    while True:
        await asyncio.sleep(_AUTO_REFRESH_INTERVAL_S)
        if not _state._bot_data.get("on_main_screen", False):
            continue
        from app.repositories import job_repo
        if job_repo.get_active_job() is None:
            continue
        try:
            text, kb = renderer.render_main_menu()
            await update_main_message(bot, text, to_telethon(kb))
        except Exception as e:
            logger.debug("Auto-refresh failed: %s", e)


# ── Bot bootstrap ──────────────────────────────────────────────────────────────

async def run_async(config: Config) -> None:
    """Build and run the Telethon bot inside an existing event loop."""
    global _bot

    bot = TelegramClient(
        StringSession(),          # ephemeral session — bot token auth needs no file
        config.TELETHON_API_ID,
        config.TELETHON_API_HASH,
    )

    _state._bot_data["admin_ids"] = config.ADMIN_IDS

    # ── /start command ─────────────────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern="/start", func=lambda e: e.is_private))
    async def on_start(event):
        uid = _get_uid(event)
        if not _is_authorized(uid, config.ADMIN_IDS):
            logger.warning("/start rejected: user %d not in ADMIN_IDS", uid)
            return
        logger.info("/start accepted for user %d", uid)
        from app.bot.handlers import start_handler
        await start_handler.start_command(bot, event)

    # ── Callback query router ──────────────────────────────────────────────────

    @bot.on(events.CallbackQuery(func=lambda e: True))
    async def on_callback(event):
        uid = _get_uid(event)
        if not _is_authorized(uid, config.ADMIN_IDS):
            return
        data = event.data.decode()
        _state._bot_data["on_main_screen"] = (data == "menu:main")

        # A pending quick-run code prompt is only ever answered by a text message.
        # Any button press means the admin navigated away (cancel, back, another
        # menu), so abandon the capture — otherwise their next message would be run
        # as code. Cleared before dispatch so a handler that re-arms it (runquick)
        # still works. Note: runquick sets awaiting_input during dispatch below.
        ud = _state.get_user_data(uid)
        if ud.get("awaiting_input") == "userbot_run_code":
            ud.pop("awaiting_input", None)
            ud.pop("run_userbot_id", None)

        try:
            if data == "menu:main":
                await event.answer()
                text, kb = renderer.render_main_menu()
                await update_main_message(bot, text, to_telethon(kb))

            elif data.startswith("page:"):
                await _handle_paging(bot, event, data)

            elif (
                data.startswith("job:")
                or data.startswith("wzd:")
                or data.startswith("je:")
                or data == "menu:jobs"
            ):
                from app.bot.handlers import job_handlers
                await job_handlers.dispatch(bot, event, uid)

            elif data.startswith("src:") or data == "menu:sources":
                from app.bot.handlers import source_handlers
                await source_handlers.dispatch_sources(bot, event, uid)

            elif data.startswith("dst:") or data == "menu:destinations":
                from app.bot.handlers import source_handlers
                await source_handlers.dispatch_destinations(bot, event, uid)

            elif data.startswith("flt:") or data == "menu:filters":
                from app.bot.handlers import filter_handlers
                await filter_handlers.dispatch(bot, event, uid)

            elif data.startswith("adm:") or data == "menu:admins":
                from app.bot.handlers import admin_handlers
                await admin_handlers.dispatch_admins(bot, event, uid)

            elif data.startswith("cfg:") or data == "menu:settings":
                from app.bot.handlers import admin_handlers
                await admin_handlers.dispatch_settings(bot, event, uid)

            elif data.startswith("ub:") or data == "menu:userbots":
                from app.bot.handlers import userbot_handlers
                await userbot_handlers.dispatch(bot, event, uid)

            elif data.startswith("hyp:"):
                from app.bot.handlers import hyper_handlers
                await hyper_handlers.dispatch(bot, event, uid)

            elif data.startswith("scr:"):
                from app.bot.handlers import script_handlers
                await script_handlers.dispatch(bot, event, uid)

            elif data == "menu:stats":
                await event.answer()
                text, kb = renderer.render_transfer_stats()
                await update_main_message(bot, text, to_telethon(kb))

            elif data.startswith("scan:") or data == "menu:scan":
                from app.bot.handlers import scan_handlers
                await scan_handlers.dispatch_scan(bot, event, uid)

            else:
                await event.answer()
                logger.warning("Unhandled callback data: %s", data)

        except Exception as e:
            logger.exception("Error handling callback %s: %s", data, e)
            try:
                text, kb = renderer.render_error(f"שגיאה פנימית: {e}")
                await update_main_message(bot, text, to_telethon(kb))
            except Exception:
                pass

    # ── Text input dispatcher ──────────────────────────────────────────────────

    @bot.on(events.NewMessage(
        func=lambda e: e.is_private and not e.message.text.startswith("/")
    ))
    async def on_text(event):
        uid = _get_uid(event)
        if not _is_authorized(uid, config.ADMIN_IDS):
            return
        ud = _state.get_user_data(uid)
        awaiting = ud.get("awaiting_input")
        if not awaiting:
            try:
                await event.delete()
            except Exception:
                pass
            return

        _dispatch_text = {
            "job_name":         ("job_handlers",    "handle_job_name"),
            "job_date_from":    ("job_handlers",    "handle_job_date_from"),
            "job_date_to":      ("job_handlers",    "handle_job_date_to"),
            "job_id_from":      ("job_handlers",    "handle_job_id_from"),
            "job_id_to":        ("job_handlers",    "handle_job_id_to"),
            "job_single_id":    ("job_handlers",    "handle_job_single_id"),
            "wzd_source_ref":   ("job_handlers",    "handle_wzd_source_ref"),
            "wzd_dest_ref":     ("job_handlers",    "handle_wzd_dest_ref"),
            "source_ref":       ("source_handlers", "handle_source_ref"),
            "dest_ref":         ("source_handlers", "handle_dest_ref"),
            "filter_word":      ("filter_handlers", "handle_filter_word"),
            "admin_id":         ("admin_handlers",  "handle_admin_id"),
            "setting_value":    ("admin_handlers",  "handle_setting_value"),
            "scan_channel_ref": ("scan_handlers",   "handle_scan_channel_ref"),
            "userbot_phone":    ("userbot_handlers", "handle_userbot_phone"),
            "userbot_code":     ("userbot_handlers", "handle_userbot_code"),
            "userbot_2fa":      ("userbot_handlers", "handle_userbot_2fa"),
            "hyper_value":      ("hyper_handlers",  "handle_hyper_value"),
            "userbot_run_code": ("userbot_handlers", "handle_userbot_run_code"),
            "script_name":      ("script_handlers", "handle_script_name"),
            "script_code":      ("script_handlers", "handle_script_code"),
        }
        entry = _dispatch_text.get(awaiting)
        if entry:
            mod_name, fn_name = entry
            import importlib
            mod = importlib.import_module(f"app.bot.handlers.{mod_name}")
            fn = getattr(mod, fn_name)
            try:
                await fn(bot, event, uid)
            except Exception as e:
                logger.exception("Error in text-input handler %s: %s", awaiting, e)
                ud.pop("awaiting_input", None)
        else:
            logger.warning("Unknown awaiting_input key: %s", awaiting)
            ud.pop("awaiting_input", None)
            try:
                await event.delete()
            except Exception:
                pass

    # ── Start the bot ──────────────────────────────────────────────────────────

    logger.info("Connecting management bot via MTProto (bot token)...")
    await bot.start(bot_token=config.BOT_TOKEN)
    _bot = bot
    me = await bot.get_me()
    logger.info("✅ Management bot connected: @%s", me.username)

    # Start auto-refresh background task
    refresh_task = asyncio.create_task(_auto_refresh_loop(bot))

    try:
        await asyncio.Event().wait()  # Run forever until cancelled
    except asyncio.CancelledError:
        pass
    finally:
        refresh_task.cancel()
        await bot.disconnect()
        logger.info("Management bot disconnected")


# ── Paging helper ──────────────────────────────────────────────────────────────

async def _handle_paging(bot: TelegramClient, event, data: str) -> None:
    await event.answer()
    parts = data.split(":")
    if len(parts) != 3:
        return
    _, screen, page_str = parts
    try:
        page = int(page_str)
    except ValueError:
        return

    bootstrap_ids: list[int] = _state._bot_data.get("admin_ids", [])

    if screen == "jobs":
        text, kb = renderer.render_job_list(page=page)
    elif screen == "sources":
        text, kb = renderer.render_source_list(page=page)
    elif screen == "destinations":
        text, kb = renderer.render_dest_list(page=page)
    elif screen == "filters":
        text, kb = renderer.render_blocked_words(page=page)
    elif screen == "admins":
        text, kb = renderer.render_admin_list(bootstrap_ids, page=page)
    elif screen == "userbots":
        text, kb = renderer.render_userbot_list(page=page)
    elif screen == "scripts":
        text, kb = renderer.render_scripts_list(page=page)
    else:
        return

    await update_main_message(bot, text, to_telethon(kb))


# ── Sync entry-point (used when mode=bot only) ────────────────────────────────

def run(config: Config) -> None:
    """Blocking run for standalone bot mode."""
    import asyncio
    asyncio.run(run_async(config))
