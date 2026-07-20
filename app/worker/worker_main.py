"""
Userbot worker: startup recovery and multi-account supervision.
Run as: python main.py worker

Job execution itself lives in userbot_manager: one runner per active userbot
account, all working in parallel. This module owns startup recovery, the
singleton "primary" duties (scans, deletes, channel resolution) and every
admin notification.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta

from telethon import TelegramClient

from app.config import Config
from app.network_errors import is_network_error
from app.repositories import job_repo, state_repo, scan_repo, userbot_repo
from app.worker.scan_engine import ScanEngine
from app.worker.telegram_utils import get_entity_safe

logger = logging.getLogger(__name__)

_shutdown_event: asyncio.Event | None = None
_resolve_trigger: asyncio.Event | None = None

# One ScanEngine per client instance (only the primary runner ever uses these).
_scan_engines: dict[int, ScanEngine] = {}


def signal_resolve_now() -> None:
    """Called from bot handlers to wake the worker for immediate channel resolution."""
    if _resolve_trigger is not None:
        _resolve_trigger.set()


def run(config: Config) -> None:
    """Entry point for the worker process (blocking)."""
    asyncio.run(_async_run(config))


# Expose for combined mode in main.py
async def run_async(config: Config) -> None:
    await _async_run(config)


async def _async_run(config: Config) -> None:
    global _shutdown_event, _resolve_trigger
    _shutdown_event = asyncio.Event()
    _resolve_trigger = asyncio.Event()

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:  # nosec B110 — intentional: Windows doesn't support add_signal_handler
            pass

    # Single-account installs keep working untouched: the .env session is
    # registered as the default account the first time the worker starts.
    userbot_repo.ensure_default(config.TELETHON_SESSION)

    _startup_recovery()

    active = userbot_repo.get_active()
    logger.info(
        "Worker starting with %d active userbot account(s): %s",
        len(active), ", ".join(u.display() for u in active) or "—",
    )

    from app.worker.userbot_manager import UserbotManager
    manager = UserbotManager(config, _shutdown_event)
    manager.set_resolve_trigger(_resolve_trigger)

    try:
        await manager.run()
    finally:
        state_repo.set_worker_status("stopped")
        _scan_engines.clear()
        logger.info("Worker stopped cleanly")


# ── Primary-account duties (scans, deletes, channel resolution) ───────────────

def _get_scan_engine(client: TelegramClient) -> ScanEngine:
    key = id(client)
    engine = _scan_engines.get(key)
    if engine is None:
        engine = ScanEngine(client)
        _scan_engines[key] = engine
    return engine


async def run_primary_duties(client: TelegramClient) -> bool:
    """
    Work that must happen on exactly one account: duplicate scans, bulk
    deletes and the heartbeat.

    Channel access checks are deliberately not here — they are per-account and
    every runner performs its own (see check_channels_for_account).

    Returns True if a scan or delete ran (so the caller should loop again
    immediately instead of sleeping).
    """
    scan_engine = _get_scan_engine(client)

    scan_task = scan_repo.get_pending_scan()
    if scan_task:
        logger.info(
            "Picked up duplicate scan #%d for channel=%s",
            scan_task["scan_id"], scan_task["channel_ref"],
        )
        try:
            await scan_engine.run_scan(scan_task["scan_id"])
            await send_scan_completion_notification(client, scan_task["scan_id"])
        except Exception as e:
            if is_network_error(e):
                logger.warning(
                    "Scan #%d: network error (%s) — resetting to pending for retry",
                    scan_task["scan_id"], e,
                )
                scan_repo.reset_running_scans_to_pending()
            else:
                logger.exception("Scan #%d: unexpected error: %s", scan_task["scan_id"], e)
                scan_repo.fail_scan(scan_task["scan_id"], str(e)[:500])
        return True

    del_task = scan_repo.get_pending_delete_job()
    if del_task:
        logger.info(
            "Picked up delete job #%d for scan_id=%d channel=%s",
            del_task["id"], del_task["scan_id"], del_task["channel_ref"],
        )
        await scan_engine.run_delete(
            del_task["id"], del_task["scan_id"], del_task["channel_ref"]
        )
        await send_delete_completion_notification(client, del_task["id"], del_task["scan_id"])
        return True

    state_repo.heartbeat()
    _requeue_stranded_jobs()
    await park_queue_if_all_capped(client)
    return False


def _requeue_stranded_jobs() -> None:
    """
    Rescue sharded jobs that finished their last chunk but were never closed.

    See job_chunk_repo.find_stranded_jobs for how a job gets into that state.
    Putting it back to 'pending' is enough: the account that claims it re-plans
    nothing (its chunks already exist and are done) and goes straight to closing
    it out, report included.
    """
    from app.repositories import job_chunk_repo

    for job_id in job_chunk_repo.find_stranded_jobs():
        logger.warning(
            "Job #%d: all chunks done but the job was never closed — re-queuing "
            "so an account can finalise it",
            job_id,
        )
        job_repo.update_status(job_id, "pending")


def _request_shutdown() -> None:
    logger.info("Shutdown signal received")
    if _shutdown_event:
        _shutdown_event.set()


def _startup_recovery() -> None:
    """
    Inspect DB state on startup and recover safely.

    Recovery cases:
    1. worker_state.status = 'running' with a current_job_id
       → The worker crashed mid-job. Re-queue the job as 'pending'.
       The copy engine will resume from last_processed_id using the
       copied_messages dedup table.

    2. Any jobs stuck in status='running' (orphaned from a previous crash
       where worker_state wasn't updated)
       → Re-queue them as 'pending'.

    3. Jobs in 'waiting_retry' are left as-is. The poll loop handles
       next_retry_at correctly.

    4. clean shutdown (idle/stopped) — just log and continue.

    5. Any userbot assignment from the previous run is stale — clear them all so
       jobs can be claimed fresh by whichever accounts come up this time. The same
       goes for job chunks: nobody holds one across a restart, and each keeps its
       own checkpoint, so a re-claimed chunk resumes instead of restarting.
    """
    logger.info("Running startup recovery...")
    ws = state_repo.get_worker_state()
    recovered = 0

    cleared = job_repo.clear_all_assignments()
    if cleared:
        logger.info("Recovery: cleared %d stale userbot assignment(s)", cleared)

    from app.repositories import job_chunk_repo
    chunks = job_chunk_repo.release_all_running()
    if chunks:
        logger.info("Recovery: released %d job chunk(s) held at shutdown", chunks)

    if ws.status == "running" and ws.current_job_id:
        job = job_repo.get_by_id(ws.current_job_id)
        if job and job.status == "running":
            logger.warning(
                "Recovery: job #%d was running at shutdown. "
                "Checkpoint: msg_id=%s. Re-queuing as pending.",
                job.id, job.last_processed_id,
            )
            job_repo.update_status(job.id, "pending")
            recovered += 1
        elif job and job.status in ("completed", "cancelled", "failed"):
            logger.info(
                "Recovery: job #%d already in terminal state '%s' — no action needed",
                job.id, job.status,
            )
    elif ws.status in ("running",):
        logger.warning("Recovery: worker_state shows running but no job_id — resetting to idle")

    # Also catch any orphaned 'running' jobs (defensive)
    orphaned = job_repo.get_all(status_filter=["running"])
    for job in orphaned:
        logger.warning(
            "Recovery: orphaned job #%d '%s' in running state — re-queuing",
            job.id, job.name,
        )
        job_repo.update_status(job.id, "pending")
        recovered += 1

    # Reset any scans stuck in 'running' state from a previous crash
    stuck_scans = scan_repo.reset_running_scans_to_pending()
    if stuck_scans:
        logger.info("Recovery: reset %d stuck scan(s) back to pending", stuck_scans)

    if recovered:
        logger.info("Recovery: re-queued %d job(s)", recovered)
    elif not stuck_scans:
        logger.info("Recovery: no action needed")

    state_repo.set_worker_status("idle")



async def _notify(chat_id_str: str | None, text: str, job_id: int, label: str) -> None:
    """Send a notification via the management bot. Shared helper for all worker notifications."""
    if not chat_id_str:
        return
    try:
        chat_id = int(chat_id_str)
    except (ValueError, TypeError):
        return
    from app.bot.bot_main import send_notification
    await send_notification(chat_id, text)
    logger.info("Job #%d: %s notification sent", job_id, label)


async def send_network_disruption_notification(
    client: TelegramClient,
    job_id: int,
    error_msg: str,
    *,
    reconnecting: bool = False,
    resumed: bool = False,
) -> None:
    job = job_repo.get_by_id(job_id)
    job_name = job.name if job else f"#{job_id}"

    if resumed:
        text = (
            f"✅ <b>ניתוק רשת — חובר מחדש</b>\n\n"
            f"📋 משימה: <b>{job_name}</b>\n"
            f"▶️ המשימה ממשיכה מנקודת ה-checkpoint האחרונה."
        )
    elif reconnecting:
        text = (
            f"⚠️ <b>ניתוק רשת במהלך משימה</b>\n\n"
            f"📋 משימה: <b>{job_name}</b>\n"
            f"🔌 הניתוק הופסק ב-checkpoint האחרון.\n"
            f"🔄 מנסה להתחבר מחדש אוטומטית..."
        )
    else:
        text = (
            f"❌ <b>ניתוק רשת — משימה נכשלה</b>\n\n"
            f"📋 משימה: <b>{job_name}</b>\n"
            f"💬 פרטים: {error_msg}"
        )

    await _notify(state_repo.get_setting("main_chat_id"), text, job_id, f"network_disruption resumed={resumed}")


async def send_no_access_notification(
    client: TelegramClient, job_id: int, tried_accounts: int
) -> None:
    """Every active userbot lacks access to this job's channels — tell the admin."""
    from app.repositories import source_repo as _src_repo

    job = job_repo.get_by_id(job_id)
    if job is None:
        return
    src = _src_repo.get_source_by_id(job.source_id)
    dst_str = ", ".join(
        d.display() if (d := _src_repo.get_destination_by_id(i)) else f"#{i}"
        for i in job.destination_id_list()
    )

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    text = (
        f"🚫 <b>אין גישה לערוץ</b>\n\n"
        f"📋 משימה: <b>{_esc(job.name)}</b>\n"
        f"📡 מקור: {_esc(src.display() if src else f'#{job.source_id}')}\n"
        f"📤 יעד: {_esc(dst_str)}\n\n"
        f"נוסו {tried_accounts} חשבונות יוזרבוט — אף אחד מהם אינו חבר בערוץ.\n"
        f"הוסף אחד מהחשבונות לערוץ והפעל את המשימה מחדש."
    )
    await _notify(state_repo.get_setting("main_chat_id"), text, job_id, "no_access")


async def send_completion_notification(client: TelegramClient, job_id: int) -> None:
    """Send job summary to the admin chat via the management bot after a job ends."""
    from app.repositories import source_repo

    job = job_repo.get_by_id(job_id)
    if not job or job.status not in ("completed", "failed"):
        return

    src = source_repo.get_source_by_id(job.source_id)

    src_str = src.display() if src else f"#{job.source_id}"
    dst_str = ", ".join(
        d.display() if (d := source_repo.get_destination_by_id(i)) else f"#{i}"
        for i in job.destination_id_list()
    )

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    status_emoji = "✅" if job.status == "completed" else "❌"
    status_word = "הושלמה" if job.status == "completed" else "נכשלה"

    report_line = ""
    if job.report_url:
        report_line = f'\n\n📋 <a href="{job.report_url}">דוח שגיאות / דילוגים</a>'

    text = (
        f"{status_emoji} <b>{_esc(job.name)}</b> — {status_word}\n\n"
        f"📡 מקור: {_esc(src_str)}\n"
        f"📤 יעד: {_esc(dst_str)}\n\n"
        f"📊 הועתקו: {job.copied_count:,} | דולגו: {job.skipped_count:,} | נכשלו: {job.failed_count:,}"
        f"{report_line}"
    )

    await _notify(state_repo.get_setting("main_chat_id"), text, job_id, "completion")


async def send_scan_completion_notification(client: TelegramClient, scan_id: int) -> None:
    """Send scan result summary to the admin chat after a scan ends."""
    scan = scan_repo.get_scan_by_id(scan_id)
    if not scan:
        return

    status = scan.get("status", "")
    channel_name = scan.get("channel_title") or scan.get("channel_ref") or "?"
    scanned = scan.get("messages_scanned", 0)
    groups = scan.get("duplicate_groups", 0)
    wasted = scan.get("wasted_count", 0)
    report_url = scan.get("report_url")
    error_msg = scan.get("error_msg") or ""

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if status == "done":
        if groups == 0:
            text = (
                f"✅ <b>סריקת כפילויות הושלמה</b>\n\n"
                f"📡 ערוץ: <b>{_esc(channel_name)}</b>\n"
                f"📊 נסרקו: <b>{scanned:,}</b> הודעות\n"
                f"🎉 לא נמצאו כפילויות!"
            )
        else:
            report_line = ""
            if report_url:
                report_line = f'\n\n📄 <a href="{report_url}">דוח מפורט עם קישורים</a>'
            text = (
                f"✅ <b>סריקת כפילויות הושלמה</b>\n\n"
                f"📡 ערוץ: <b>{_esc(channel_name)}</b>\n"
                f"📊 נסרקו: <b>{scanned:,}</b> הודעות\n"
                f"🔁 קבוצות כפולות: <b>{groups:,}</b>\n"
                f"🗑 ניתן למחוק: <b>{wasted:,}</b> הודעות"
                f"{report_line}"
            )
    else:
        text = (
            f"❌ <b>סריקת כפילויות נכשלה</b>\n\n"
            f"📡 ערוץ: <b>{_esc(channel_name)}</b>\n"
            f"💬 שגיאה: {_esc(error_msg[:200])}"
        )

    chat_id_str = state_repo.get_setting("main_chat_id")
    if not chat_id_str:
        return
    try:
        chat_id = int(chat_id_str)
    except (ValueError, TypeError):
        return
    from app.bot.bot_main import send_notification
    await send_notification(chat_id, text)
    logger.info("Scan #%d: completion notification sent", scan_id)


async def send_delete_completion_notification(
    client: TelegramClient, delete_job_id: int, scan_id: int
) -> None:
    """Send bulk-delete result summary to the admin chat."""
    scan = scan_repo.get_scan_by_id(scan_id)
    channel_name = (scan or {}).get("channel_title") or (scan or {}).get("channel_ref") or "?"

    from app.repositories.scan_repo import get_latest_delete_job
    del_job = get_latest_delete_job(scan_id)
    if not del_job:
        return

    del_status = del_job.get("status", "")
    deleted = del_job.get("deleted_count", 0)
    error_msg = del_job.get("error_msg") or ""

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if del_status == "done":
        text = (
            f"🗑 <b>מחיקת כפילויות הושלמה</b>\n\n"
            f"📡 ערוץ: <b>{_esc(channel_name)}</b>\n"
            f"✅ נמחקו: <b>{deleted:,}</b> הודעות"
        )
    else:
        text = (
            f"❌ <b>מחיקת כפילויות נכשלה</b>\n\n"
            f"📡 ערוץ: <b>{_esc(channel_name)}</b>\n"
            f"💬 שגיאה: {_esc(error_msg[:200])}"
        )

    chat_id_str = state_repo.get_setting("main_chat_id")
    if not chat_id_str:
        return
    try:
        chat_id = int(chat_id_str)
    except (ValueError, TypeError):
        return
    from app.bot.bot_main import send_notification
    await send_notification(chat_id, text)
    logger.info("Delete job #%d: completion notification sent", delete_job_id)


def account_is_capped(userbot_id: int) -> bool:
    """
    True once this account has copied its DAILY_LIMIT for the day.

    Telegram enforces its limits per account, so the cap is a fact about the
    *account*, never about the job: a capped account simply stops claiming work
    (see UserbotRunner._loop) while the others keep going at full speed. Only
    when every active account is capped does the queue wait — park_queue_if_all_capped.
    """
    from app.ui.texts import DAILY_LIMIT

    return job_repo.get_daily_count_for_userbot(userbot_id) >= DAILY_LIMIT


def _next_midnight_il() -> datetime:
    from datetime import timezone
    from zoneinfo import ZoneInfo

    now_il = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Jerusalem"))
    return (now_il + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


async def park_queue_if_all_capped(client: TelegramClient) -> None:
    """
    Hold the queue until midnight only when *no* account has budget left.

    Without this the queue would sit at 'pending' with nobody able to claim it and
    no word to the admin. With it, the wait is explicit and announced — and it can
    never happen while a single account still has quota to spend.
    """
    from app.ui.texts import DAILY_LIMIT

    active = userbot_repo.get_active()
    if not active:
        return
    if any(not account_is_capped(u.id) for u in active):
        return

    parked = job_repo.get_all(status_filter=["pending"])
    if not parked:
        return

    next_midnight_il = _next_midnight_il()
    from datetime import timezone
    retry_at = next_midnight_il.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    for job in parked:
        job_repo.update_status(
            job.id,
            "waiting_retry",
            error=f"הגבלה יומית: כל החשבונות העבירו {DAILY_LIMIT:,} הודעות היום",
            next_retry_at=retry_at,
        )
        logger.warning(
            "Job #%d: every active account (%d) is out of daily quota — parked until %s",
            job.id, len(active), retry_at,
        )
        await send_daily_limit_notification(client, job.id, next_midnight_il)


async def send_daily_limit_notification(
    client: TelegramClient, job_id: int, next_midnight_il
) -> None:
    """Tell the admin the queue is out of daily quota on every account."""
    from app.ui.texts import DAILY_LIMIT, esc

    job = job_repo.get_by_id(job_id)
    job_name = esc(job.name) if job else f"#{job_id}"
    resume_time = next_midnight_il.strftime("%d/%m/%Y 00:00")

    accounts = "\n".join(
        f"🔸 {esc(ub.display())}: <b>{job_repo.get_daily_count_for_userbot(ub.id):,}</b>"
        f" / {DAILY_LIMIT:,}"
        for ub in userbot_repo.get_active()
    )

    text = (
        f"⏸ <b>הגבלה יומית הושגה</b>\n\n"
        f"📋 משימה: <b>{job_name}</b>\n\n"
        f"כל חשבונות היוזרבוט מיצו את המכסה היומית:\n"
        f"{accounts}\n\n"
        f"🕛 המשימה תמשיך אוטומטית מחר בחצות ({resume_time}).\n"
        f"<i>הוספת חשבון נוסף תגדיל את המכסה הכוללת.</i>"
    )

    await _notify(state_repo.get_setting("main_chat_id"), text, job_id, "daily_limit")


async def check_channels_for_account(client: TelegramClient, userbot_id: int) -> None:
    """
    Probe every source/destination this account hasn't checked yet and record
    whether it can reach them, so the UI can report access per account.

    Access is a per-account fact: one userbot may be a member of a channel while
    another is not. Every active account therefore runs this for itself, and the
    first one that gets through also fills in the channel's title, ID and extra
    info — a channel is resolvable as long as *some* account can see it.
    """
    from app.repositories import channel_access_repo, source_repo

    pending = channel_access_repo.get_unchecked_channels(userbot_id)
    if not pending:
        return

    for kind, channel_id, channel_ref in pending:
        is_source = kind == channel_access_repo.KIND_SOURCE
        channel = (
            source_repo.get_source_by_id(channel_id)
            if is_source
            else source_repo.get_destination_by_id(channel_id)
        )
        if channel is None:
            continue  # deleted while we were working through the list

        try:
            entity = await get_entity_safe(client, channel_ref)
            # get_entity alone resolves public channels without membership; read one
            # message so the probe reflects what a job would actually be able to do.
            await client.get_messages(entity, limit=1)
        except Exception as e:
            channel_access_repo.record(kind, channel_id, userbot_id, False, str(e)[:300])
            logger.info(
                "Userbot #%d has no access to %s '%s' (%s): %s",
                userbot_id, kind, channel.name, channel_ref, e,
            )
            _mark_unreachable_if_nobody_has_access(kind, channel_id, str(e))
            continue

        channel_access_repo.record(kind, channel_id, userbot_id, True, None)
        logger.info(
            "Userbot #%d has access to %s '%s'", userbot_id, kind, channel.name
        )

        if channel.resolved_id is None:
            await _resolve_channel_info(client, kind, channel_id, entity, channel_ref)


async def _resolve_channel_info(
    client: TelegramClient, kind: str, channel_id: int, entity, channel_ref: str
) -> None:
    """Fill in title, ID and extra info for a channel this account can reach."""
    from app.repositories import channel_access_repo, source_repo

    is_source = kind == channel_access_repo.KIND_SOURCE
    title = getattr(entity, "title", channel_ref)

    if is_source:
        source_repo.update_source_resolved(channel_id, title, entity.id)
        source_repo.update_source_name(channel_id, title)
        source_repo.set_source_validation_error(channel_id, None)
    else:
        source_repo.update_destination_resolved(channel_id, title, entity.id)
        source_repo.update_destination_name(channel_id, title)
        source_repo.set_dest_validation_error(channel_id, None)
    logger.info("Resolved %s '%s': %s (id=%d)", kind, channel_ref, title, entity.id)

    # Metadata is a bonus — the channel is already resolved and reachable without it.
    try:
        extra = await _fetch_channel_extra_info(client, entity)
    except Exception as e:
        logger.warning("Could not fetch extra info for %s '%s': %s", kind, channel_ref, e)
        return
    if is_source:
        source_repo.update_source_extra_info(channel_id, **extra)
    else:
        source_repo.update_destination_extra_info(channel_id, **extra)


def _mark_unreachable_if_nobody_has_access(kind: str, channel_id: int, error: str) -> None:
    """
    Flag the channel as inaccessible only once every active account has tried and
    failed — a single account's failure says nothing while others may still get in.
    """
    from app.repositories import channel_access_repo, source_repo

    if channel_access_repo.any_active_has_access(kind, channel_id):
        return
    if channel_access_repo.pending_active_checks(kind, channel_id) > 0:
        return

    if kind == channel_access_repo.KIND_SOURCE:
        source_repo.set_source_validation_error(channel_id, error)
    else:
        source_repo.set_dest_validation_error(channel_id, error)


async def _fetch_channel_extra_info(client: TelegramClient, entity) -> dict:
    """Fetch additional channel metadata. Returns a dict ready for update_*_extra_info."""
    from telethon.tl.types import (
        InputMessagesFilterPhotos,
        InputMessagesFilterVideo,
        InputMessagesFilterDocument,
    )

    username = getattr(entity, "username", None)
    participants_count = getattr(entity, "participants_count", None)
    about = getattr(entity, "about", None)
    verified = bool(getattr(entity, "verified", False))

    if getattr(entity, "broadcast", False):
        channel_type = "ערוץ"
    elif getattr(entity, "megagroup", False):
        channel_type = "קבוצת-על"
    elif getattr(entity, "gigagroup", False):
        channel_type = "קהילה"
    else:
        channel_type = "קבוצה"

    total_messages = photos_count = videos_count = docs_count = None
    try:
        msgs = await client.get_messages(entity, limit=1)
        total_messages = msgs.total
    except Exception:  # nosec B110 — optional metadata, failure is non-fatal
        pass
    try:
        msgs = await client.get_messages(entity, limit=1, filter=InputMessagesFilterPhotos)
        photos_count = msgs.total
    except Exception:  # nosec B110 — optional metadata, failure is non-fatal
        pass
    try:
        msgs = await client.get_messages(entity, limit=1, filter=InputMessagesFilterVideo)
        videos_count = msgs.total
    except Exception:  # nosec B110 — optional metadata, failure is non-fatal
        pass
    try:
        msgs = await client.get_messages(entity, limit=1, filter=InputMessagesFilterDocument)
        docs_count = msgs.total
    except Exception:  # nosec B110 — optional metadata, failure is non-fatal
        pass

    return {
        "username": username,
        "participants_count": participants_count,
        "about": about,
        "verified": verified,
        "channel_type": channel_type,
        "total_messages": total_messages,
        "photos_count": photos_count,
        "videos_count": videos_count,
        "docs_count": docs_count,
    }
