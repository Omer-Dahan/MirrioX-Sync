"""
Duplicate scan engine: scans a channel for duplicate media files
and optionally deletes duplicate messages via the userbot.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable, Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetHistoryRequest

from app.repositories import scan_repo
from app.worker.telegram_utils import get_entity_safe

logger = logging.getLogger(__name__)

_DELETE_BATCH_SIZE = 100   # Telegram allows up to 100 deletions per call
_DELETE_SLEEP_S    = 1.5   # pause between delete batches
_MSG_SLEEP_S       = 0.5   # pause between each fetched message
_BATCH_SLEEP_S     = 10.0  # longer pause every _BATCH_EVERY messages
_BATCH_EVERY       = 50    # messages between batch pauses
_PROGRESS_EVERY    = 10    # update DB progress every N messages


class ScanEngine:
    def __init__(self, client: TelegramClient, resolve_callback: Optional[Callable[[], Awaitable[None]]] = None) -> None:
        self._client = client
        self._resolve_callback = resolve_callback

    # -----------------------------------------------------------------------
    # Public entry points
    # -----------------------------------------------------------------------

    async def run_scan(self, scan_id: int) -> None:
        """Iterate messages in the channel and record media IDs for dedup analysis.

        On the first scan (no prior completed scans for the channel), all messages
        are fetched from the beginning of the channel history.

        On subsequent scans, only messages added after the last_scanned_message_id
        are fetched from Telegram (incremental / delta mode). New items are
        cross-checked against all media_ids from previous scans so that
        cross-scan duplicates are detected without re-fetching old messages.
        """
        scan_data = scan_repo.get_scan_by_id(scan_id)
        if scan_data is None:
            logger.error("Scan #%d not found in DB", scan_id)
            return

        channel_ref = scan_data["channel_ref"]
        channel_title = scan_data.get("channel_title") or channel_ref

        scan_repo.start_scan(scan_id)
        logger.info("Scan #%d: starting for channel=%s", scan_id, channel_ref)

        try:
            entity = await get_entity_safe(self._client, channel_ref)
            if entity is None:
                scan_repo.fail_scan(scan_id, f"לא ניתן לפתור ערוץ: {channel_ref}")
                return

            # Determine starting point for incremental scan
            last_message_id = scan_repo.get_last_scanned_message_id(channel_ref)
            is_incremental = last_message_id > 0

            if is_incremental:
                logger.info(
                    "Scan #%d: incremental mode — scanning only messages after #%d",
                    scan_id, last_message_id,
                )
                # Load all previously known media_ids for cross-scan duplicate detection.
                # This allows detecting "old original + new copy" duplicates without
                # re-scanning the entire channel.
                known_media_ids: set[int] = scan_repo.get_all_known_media_ids_for_channel(channel_ref)
                logger.info(
                    "Scan #%d: loaded %d known media_ids from previous scans",
                    scan_id, len(known_media_ids),
                )
            else:
                # Full scan: no prior data; start fresh
                known_media_ids = set()

            total = await self._get_total_messages(entity)
            scan_repo.update_progress(scan_id, 0, total)

            scanned = 0
            from app.db import get_connection
            conn = get_connection()

            # Use min_id for incremental scan to only fetch new messages
            iter_kwargs: dict = {"reverse": True}
            if is_incremental:
                iter_kwargs["min_id"] = last_message_id

            iter_obj = self._client.iter_messages(entity, **iter_kwargs)
            while True:
                try:
                    msg = await iter_obj.__anext__()
                except StopAsyncIteration:
                    break
                except FloodWaitError as e:
                    wait_s = e.seconds + 5
                    logger.warning("Scan #%d: FloodWait %ds — sleeping", scan_id, wait_s)
                    await asyncio.sleep(wait_s)
                    continue

                if not msg or not msg.media:
                    scanned += 1
                    await asyncio.sleep(_MSG_SLEEP_S)
                    if scanned % _PROGRESS_EVERY == 0:
                        scan_repo.update_progress(scan_id, scanned, total)
                        if self._resolve_callback:
                            await self._resolve_callback()
                    if scanned % _BATCH_EVERY == 0:
                        await asyncio.sleep(_BATCH_SLEEP_S)
                    continue

                media_id, media_type, file_size, mime_type = self._extract_media_info(msg)
                if media_id is None:
                    scanned += 1
                    await asyncio.sleep(_MSG_SLEEP_S)
                    if scanned % _PROGRESS_EVERY == 0:
                        scan_repo.update_progress(scan_id, scanned, total)
                        if self._resolve_callback:
                            await self._resolve_callback()
                    if scanned % _BATCH_EVERY == 0:
                        await asyncio.sleep(_BATCH_SLEEP_S)
                    continue

                msg_date = msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else ""

                import sqlite3
                try:
                    scan_repo.insert_item(
                        scan_id=scan_id,
                        message_id=msg.id,
                        media_id=media_id,
                        media_type=media_type,
                        file_size=file_size,
                        mime_type=mime_type,
                        msg_date=msg_date,
                    )
                except sqlite3.IntegrityError:
                    logger.warning("Scan #%d deleted mid-run, aborting", scan_id)
                    scan_repo.fail_scan(scan_id, "סריקה נמחקה במהלך הפעולה")
                    break

                # Track media_ids to detect duplicates within this batch and
                # (in incremental mode) against previously scanned data.
                known_media_ids.add(media_id)

                scanned += 1
                await asyncio.sleep(_MSG_SLEEP_S)
                if scanned % _PROGRESS_EVERY == 0:
                    conn.commit()
                    scan_repo.update_progress(scan_id, scanned, total)
                    if self._resolve_callback:
                        await self._resolve_callback()
                if scanned % _BATCH_EVERY == 0:
                    conn.commit()
                    await asyncio.sleep(_BATCH_SLEEP_S)

            # Final commit and progress update
            conn.commit()
            scan_repo.update_progress(scan_id, scanned, total)

            # Compute duplicate groups across ALL scans for this channel
            groups = scan_repo.get_duplicate_groups(scan_id)
            wasted = sum(g["total_count"] - 1 for g in groups)

            report_url: str | None = None
            if groups:
                report_url = await self._build_report(scan_id, groups, channel_ref, channel_title)

            scan_repo.finish_scan(scan_id, len(groups), wasted, report_url)
            logger.info(
                "Scan #%d done: scanned=%d groups=%d wasted=%d incremental=%s",
                scan_id, scanned, len(groups), wasted, is_incremental,
            )

        except asyncio.CancelledError:
            scan_repo.fail_scan(scan_id, "Scan cancelled")
            raise
        except Exception as exc:
            logger.exception("Scan #%d failed: %s", scan_id, exc)
            scan_repo.fail_scan(scan_id, str(exc)[:500])

    async def run_delete(self, delete_job_id: int, scan_id: int, channel_ref: str) -> None:
        """Delete all duplicate messages (keeping the oldest in each group)."""
        scan_repo.start_delete_job(delete_job_id)
        logger.info("Delete job #%d: starting (scan_id=%d)", delete_job_id, scan_id)

        try:
            entity = await get_entity_safe(self._client, channel_ref)
            if entity is None:
                scan_repo.fail_delete_job(delete_job_id, f"לא ניתן לפתור ערוץ: {channel_ref}")
                return

            groups = scan_repo.get_duplicate_groups(scan_id)
            if not groups:
                scan_repo.finish_delete_job(delete_job_id, 0)
                return

            ids_to_delete: list[int] = []
            for group in groups:
                items = scan_repo.get_items_for_media(scan_id, group["media_id"])
                # keep first (oldest), delete rest
                for item in items[1:]:
                    ids_to_delete.append(item["message_id"])

            total_deleted = 0
            for i in range(0, len(ids_to_delete), _DELETE_BATCH_SIZE):
                batch = ids_to_delete[i : i + _DELETE_BATCH_SIZE]
                try:
                    await self._client.delete_messages(entity, batch)
                    total_deleted += len(batch)
                    logger.info(
                        "Delete job #%d: deleted batch %d-%d (%d total)",
                        delete_job_id, i, i + len(batch), total_deleted,
                    )
                except Exception as exc:
                    logger.warning("Delete job #%d: batch error: %s", delete_job_id, exc)
                if self._resolve_callback:
                    await self._resolve_callback()
                await asyncio.sleep(_DELETE_SLEEP_S)

            scan_repo.finish_delete_job(delete_job_id, total_deleted)
            logger.info("Delete job #%d done: deleted %d messages", delete_job_id, total_deleted)

        except asyncio.CancelledError:
            scan_repo.fail_delete_job(delete_job_id, "Delete cancelled")
            raise
        except Exception as exc:
            logger.exception("Delete job #%d failed: %s", delete_job_id, exc)
            scan_repo.fail_delete_job(delete_job_id, str(exc)[:500])

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _get_total_messages(self, entity: Any) -> int:
        for attempt in range(3):
            try:
                result = await self._client(GetHistoryRequest(
                    peer=entity,
                    offset_id=0,
                    offset_date=None,
                    add_offset=0,
                    limit=1,
                    max_id=0,
                    min_id=0,
                    hash=0,
                ))
                return getattr(result, "count", 0) or 0
            except FloodWaitError as e:
                wait_s = e.seconds + 5
                logger.warning("_get_total_messages: FloodWait %ds (attempt %d)", wait_s, attempt + 1)
                await asyncio.sleep(wait_s)
            except Exception as exc:
                logger.warning("Could not fetch total message count: %s", exc)
                return 0
        return 0

    @staticmethod
    def _extract_media_info(msg: Any) -> tuple[int | None, str, int | None, str | None]:
        media = msg.media
        type_name = type(media).__name__

        if type_name == "MessageMediaDocument":
            doc = getattr(media, "document", None)
            if doc is None:
                return None, "", None, None
            return doc.id, "document", getattr(doc, "size", None), getattr(doc, "mime_type", None)

        if type_name == "MessageMediaPhoto":
            photo = getattr(media, "photo", None)
            if photo is None:
                return None, "", None, None
            size = None
            for s in getattr(photo, "sizes", []):
                sz = getattr(s, "size", None)
                if sz and (size is None or sz > size):
                    size = sz
            return photo.id, "photo", size, "image/jpeg"

        return None, "", None, None

    async def _build_report(
        self,
        scan_id: int,
        groups: list[dict[str, Any]],
        channel_ref: str,
        channel_title: str,
    ) -> str | None:
        from app.services import telegraph_service
        from app.repositories.scan_repo import get_items_for_media

        # Try to get resolved_id and username from destinations table first, then sources
        from app.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT username, resolved_id FROM destinations WHERE channel_ref=? LIMIT 1",
            (channel_ref,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT username, resolved_id FROM sources WHERE channel_ref=? LIMIT 1",
                (channel_ref,),
            ).fetchone()

        username = row["username"] if row else None
        resolved_id = row["resolved_id"] if row else None

        return await telegraph_service.create_duplicates_report(
            scan_id=scan_id,
            groups=groups,
            get_items_fn=lambda media_id: get_items_for_media(scan_id, media_id),
            username=username,
            resolved_id=resolved_id,
        )
