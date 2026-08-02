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
from app.repositories import admin_repo, state_repo
from app.bot import state as _state
from app.bot.handlers._common import (
    update_main_message,
    answer_callback,
    edits_are_rate_limited,
)
from app.ui import keyboards, renderer
from app.ui.keyboards import to_telethon

logger = logging.getLogger(__name__)

_AUTO_REFRESH_INTERVAL_S = 30

# Module-level bot reference — set once at startup.
# Used by the worker to send proactive notifications.
_bot: TelegramClient | None = None
_admin_chat_id: int | None = None  # chat to send notifications to


# ── Notification helper ────────────────────────────────────────────────────────

# In combined mode the worker starts copying a second or two before the bot
# finishes connecting, so the first alerts of a run were written to the log and
# thrown away — which is how the two most important messages of a failing job
# went missing. Waiting is only right when a bot is actually on its way up:
# under `main.py worker` there is no bot in this process and never will be, and
# waiting there would stall the worker for the timeout on every single alert.
_BOT_READY_WAIT_S = 30
_bot_starting = False
_bot_ready: asyncio.Event | None = None


def _ready_event() -> asyncio.Event:
    """The "bot is connected" flag, created lazily so it binds to the running loop."""
    global _bot_ready
    if _bot_ready is None:
        _bot_ready = asyncio.Event()
        if _bot is not None:
            _bot_ready.set()
    return _bot_ready


async def _wait_until_ready() -> bool:
    """Give a still-connecting bot a moment. False if there is not one coming."""
    if _bot is not None:
        return True
    if not _bot_starting:
        return False
    try:
        await asyncio.wait_for(_ready_event().wait(), timeout=_BOT_READY_WAIT_S)
    except asyncio.TimeoutError:
        return False
    return _bot is not None


async def send_notification(chat_id: int, text: str) -> None:
    """Send a message via the management bot (MTProto)."""
    if not await _wait_until_ready():
        logger.error(
            "send_notification: no management bot in this process — alert dropped: %s",
            text[:120].replace("\n", " "),
        )
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


def _restore_panel(chat_id: str | None, msg_id: str | None) -> None:
    """Undo a panel adoption whose edit turned out to fail.

    update_main_message() clears the stored coordinates when the message is
    dead. If we had just adopted the clicked message, the previously known
    panel may still be alive, so put it back rather than leaving the bot with
    no panel at all.
    """
    if chat_id and msg_id:
        state_repo.set_setting("main_message_id", msg_id)
        state_repo.set_setting("main_chat_id", chat_id)


# ── Latency instrumentation ────────────────────────────────────────────────────

# A callback slower than this is logged with its timing breakdown. Telegram
# expires a callback query after ~15s, so anything near that already means the
# user saw a dead button.
_SLOW_CALLBACK_S = 3.0

# How far behind schedule the watchdog's own sleep may run before it reports.
# The loop is shared with the worker, so a stall here means the worker blocked
# it — and every button press queued behind that block.
_LOOP_STALL_S = 2.0


async def _loop_stall_watchdog() -> None:
    """Report when the shared event loop stops running on time.

    The bot and worker live in one event loop, so a synchronous or long-awaited
    operation in the worker delays every callback the bot is trying to answer.
    That delay is invisible in the handler's own timing — it happens before the
    handler is even scheduled — so it is measured here instead: sleep a known
    interval and log however much longer it actually took.
    """
    interval = 1.0
    while True:
        before = asyncio.get_running_loop().time()
        await asyncio.sleep(interval)
        drift = asyncio.get_running_loop().time() - before - interval
        if drift >= _LOOP_STALL_S:
            logger.warning(
                "Event loop stalled %.1fs (shared with worker) — "
                "button presses during this window were queued, not lost",
                drift,
            )


# ── Auto-refresh ───────────────────────────────────────────────────────────────

async def _auto_refresh_loop(bot: TelegramClient) -> None:
    """Periodically refresh the main menu while the main screen is visible."""
    last_text: str | None = None
    while True:
        await asyncio.sleep(_AUTO_REFRESH_INTERVAL_S)
        if not _state._bot_data.get("on_main_screen", False):
            continue
        from app.repositories import job_repo
        if job_repo.get_active_job() is None:
            continue
        if edits_are_rate_limited():
            continue  # Telegram is throttling panel edits — don't add to the queue
        try:
            text, kb = renderer.render_main_menu()
            # Skip an edit that would change nothing. A long-running job can hold
            # the panel identical for many cycles, and every one of those edits
            # counted against Telegram's per-message edit limit for no benefit —
            # which is what pushed the panel into FloodWait in the first place.
            if text == last_text:
                continue
            last_text = text
            # A False result means the panel is gone and its coordinates were
            # cleared; on_main_screen is now False, so the loop goes quiet
            # until the next /start instead of retrying a dead id every 30s.
            await update_main_message(bot, text, to_telethon(kb))
        except Exception as e:
            logger.debug("Auto-refresh failed: %s", e)


# ── Bot bootstrap ──────────────────────────────────────────────────────────────

async def run_async(config: Config) -> None:
    """Build and run the Telethon bot inside an existing event loop."""
    global _bot, _bot_starting

    # Claimed before the connect: a worker alert raised during those few seconds
    # should wait for us rather than be dropped.
    _bot_starting = True

    bot = TelegramClient(
        StringSession(),          # ephemeral session — bot token auth needs no file
        config.TELETHON_API_ID,
        config.TELETHON_API_HASH,
        # Telethon's default (60) makes it swallow any FloodWait under a minute
        # and sleep inside the call — silently, with no exception and no log. A
        # panel edit that hits Telegram's per-message edit limit would block the
        # whole handler for up to 60s, long past the ~15s at which Telegram
        # expires the callback query, so the button looked dead or answered very
        # late. 0 makes the error surface immediately, where it can be logged and
        # returned from. Matches the worker's client (see userbot_manager).
        flood_sleep_threshold=0,
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

        # Timing baseline. `age` is how long the press sat before this handler ran
        # — it comes from Telegram's own timestamp on the button press, so it
        # captures queueing in the shared event loop that the handler itself
        # cannot see. `t0` measures the handler's own work.
        t0 = asyncio.get_running_loop().time()
        press_age = None
        msg_date = getattr(getattr(event, "query", None), "date", None) or getattr(event, "date", None)
        if msg_date is not None:
            try:
                from datetime import datetime, timezone
                press_age = (datetime.now(timezone.utc) - msg_date).total_seconds()
            except Exception:
                press_age = None

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
            # Sync the active panel to the one the user just interacted with, so
            # clicking an older panel adopts it instead of updating a different
            # one. The previous coordinates are kept so a failed edit can roll
            # back: without that, clicking a deleted ghost panel writes a dead
            # message id into the DB and freezes the live panel as well.
            prev_msg_id = state_repo.get_setting("main_message_id")
            prev_chat_id = state_repo.get_setting("main_chat_id")
            adopted = False
            if getattr(event, "message_id", None) and str(event.message_id) != prev_msg_id:
                state_repo.set_setting("main_message_id", str(event.message_id))
                state_repo.set_setting("main_chat_id", str(event.chat_id))
                adopted = True

            if data == "menu:main":
                await answer_callback(event)
                text, kb = renderer.render_main_menu()
                ok = await update_main_message(bot, text, to_telethon(kb))
                if not ok and adopted:
                    _restore_panel(prev_chat_id, prev_msg_id)

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
                await answer_callback(event)
                text, kb = renderer.render_transfer_stats()
                ok = await update_main_message(bot, text, to_telethon(kb))
                if not ok and adopted:
                    _restore_panel(prev_chat_id, prev_msg_id)

            elif data.startswith("scan:") or data == "menu:scan":
                from app.bot.handlers import scan_handlers
                await scan_handlers.dispatch_scan(bot, event, uid)

            else:
                await answer_callback(event)
                logger.warning("Unhandled callback data: %s", data)

            # A dispatched handler may have found the adopted message dead and
            # forgotten it (see update_main_message/forget_main_message). The
            # previously tracked panel may still be alive, so put it back
            # instead of leaving the bot with no panel at all. This covers
            # every branch above, not just the two that check `ok` inline.
            if adopted and not state_repo.get_setting("main_message_id"):
                _restore_panel(prev_chat_id, prev_msg_id)

        except Exception as e:
            logger.exception("Error handling callback %s: %s", data, e)
            try:
                text, kb = renderer.render_error(f"שגיאה פנימית: {e}")
                await update_main_message(bot, text, to_telethon(kb))
            except Exception:
                pass

        finally:
            # Split the delay into "waited to be scheduled" vs "spent working".
            # A large press_age with a small handler time means the loop was
            # blocked elsewhere (the worker); the reverse means this handler's
            # own DB query or Telegram call is the slow part.
            handler_s = asyncio.get_running_loop().time() - t0
            if handler_s >= _SLOW_CALLBACK_S or (press_age or 0) >= _SLOW_CALLBACK_S:
                logger.warning(
                    "Slow callback %s: waited %s before dispatch, %.1fs in handler",
                    data,
                    "%.1fs" % press_age if press_age is not None else "unknown",
                    handler_s,
                )
            else:
                logger.debug("Callback %s handled in %.2fs", data, handler_s)

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
    # Releases any worker notification that arrived while we were connecting.
    _ready_event().set()
    me = await bot.get_me()
    logger.info("✅ Management bot connected: @%s", me.username)

    # Start auto-refresh background task
    refresh_task = asyncio.create_task(_auto_refresh_loop(bot))
    # Reports when the worker blocks the shared loop and delays button handling.
    watchdog_task = asyncio.create_task(_loop_stall_watchdog())

    try:
        await asyncio.Event().wait()  # Run forever until cancelled
    except asyncio.CancelledError:
        pass
    finally:
        refresh_task.cancel()
        watchdog_task.cancel()
        await bot.disconnect()
        logger.info("Management bot disconnected")


# ── Paging helper ──────────────────────────────────────────────────────────────

async def _handle_paging(bot: TelegramClient, event, data: str) -> None:
    await answer_callback(event)
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
