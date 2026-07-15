"""Core copy logic using Telethon. Executes a single job end-to-end."""
# pylint: disable=too-many-branches,too-many-statements,too-many-locals
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import AsyncIterator, Optional, Callable, Awaitable
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    ChannelPrivateError,
    ChatForwardsRestrictedError,
)
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.tl.types import (
    Message,
    MessageMediaUnsupported,
)

from app.models import Job, NoAccessError
from app.repositories import job_repo, filter_repo, source_repo, dedup_repo
from app.worker.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Job date bounds are entered in Israel local time, matching the rest of the app
# (daily limits, transfer stats).
_IL_TZ = ZoneInfo("Asia/Jerusalem")


class CopyEngine:
    """Executes a copy job using the provided Telethon client."""

    def __init__(
        self,
        client: TelegramClient,
        resolve_callback: Optional[Callable[[], Awaitable[None]]] = None,
        userbot_id: Optional[int] = None,
    ) -> None:
        self._client = client
        self._rate_limiter = RateLimiter()
        self._resolve_callback = resolve_callback
        self._userbot_id = userbot_id

    async def run_job(self, job: Job) -> None:
        from app.repositories import state_repo
        settings = state_repo.get_settings_dict()
        self._rate_limiter.update_from_settings(settings)
        group_media: bool = job.group_media
        skip_duplicates: bool = settings.get("skip_duplicates", "0") == "1"

        # Snapshot blocked words once at job start
        blocked_words: list[str] = []
        if job.use_blocked_words:
            blocked_words = filter_repo.get_word_strings()
            logger.info("Job #%d: %d blocked words loaded", job.id, len(blocked_words))

        src_rec = source_repo.get_source_by_id(job.source_id)
        dst_rec = source_repo.get_destination_by_id(job.destination_id)
        if not src_rec or not dst_rec:
            job_repo.update_status(job.id, "failed", error="מקור או יעד לא נמצאו")
            return

        try:
            from app.worker.telegram_utils import get_entity_safe
            src_entity = await get_entity_safe(
                self._client, str(src_rec.resolved_id or src_rec.channel_ref)
            )
            dst_entity = await get_entity_safe(
                self._client, str(dst_rec.resolved_id or dst_rec.channel_ref)
            )
        except (ChannelPrivateError, ValueError) as e:
            # This account cannot see the channel. Let the worker offer the job to
            # another userbot instead of failing it outright.
            logger.warning(
                "Job #%d: userbot %s has no access (%s) — requesting reassignment",
                job.id, self._userbot_id, e,
            )
            raise NoAccessError(f"אין גישה לערוץ: {e}") from e

        # Save resolved IDs for future use
        if not src_rec.resolved_id:
            try:
                source_repo.update_source_resolved(
                    src_rec.id,
                    getattr(src_entity, "title", src_rec.channel_ref),
                    src_entity.id,
                )
            except Exception:  # nosec B110 — best-effort cache update, non-fatal
                pass

        if not dst_rec.resolved_id:
            try:
                source_repo.update_destination_resolved(
                    dst_rec.id,
                    getattr(dst_entity, "title", dst_rec.channel_ref),
                    dst_entity.id,
                )
            except Exception:  # nosec B110 — best-effort cache update, non-fatal
                pass

        # Build dedup set from DB
        already_done: set[int] = job_repo.get_copied_source_ids(job.id)
        logger.info(
            "Job #%d: resuming — %d already done, checkpoint=#%s",
            job.id, len(already_done), job.last_processed_id,
        )

        # Detected once per job: if True, skip ForwardMessagesRequest entirely
        src_is_protected: bool = False

        job_repo.mark_started(job.id)

        copied = job.copied_count
        skipped = job.skipped_count
        failed = job.failed_count
        _last_progress_log = copied
        _msgs_since_pause_check = 0  # check for pause every 25 messages
        _msgs_since_limit_check = 0  # check daily limit every 100 messages

        # Buffer for collecting media-group messages before forwarding them together
        pending_group: list[Message] = []
        current_group_id: Optional[int] = None

        # Buffer for grouping individually-sent photos/videos into albums (group_media feature)
        solo_media_buffer: list[Message] = []

        async def flush_solo_media() -> bool:
            """Flush solo media buffer. Returns True if the job must stop (paused/cancelled)."""
            nonlocal copied, skipped, failed, solo_media_buffer, src_is_protected
            if not solo_media_buffer:
                return False
            # Check before sending, not only after: otherwise a cancel still lets
            # one more album go out.
            if job_repo.should_stop(job.id):
                return True
            buffer = solo_media_buffer[:]
            solo_media_buffer = []
            # Safe to checkpoint at the end of the buffer only while every message
            # in it has been handled. The album-failure path below re-queues some
            # of them and lowers this accordingly.
            checkpoint = buffer[-1].id

            # Apply per-message filters; collect messages that should be sent
            allowed_types: set[str] = set((job.content_types or "text,image,video").split(","))
            to_send: list[Message] = []
            for m in buffer:
                if not job.copy_text and (not m.media or isinstance(m.media, MessageMediaUnsupported)):
                    job_repo.record_copied_message(job.id, m.id, None, "skipped", "text_stripped_empty", userbot_id=self._userbot_id)
                    already_done.add(m.id)
                    skipped += 1
                    continue
                if blocked_words and self._is_blocked(m, blocked_words):
                    job_repo.record_copied_message(job.id, m.id, None, "skipped", "blocked_word", userbot_id=self._userbot_id)
                    already_done.add(m.id)
                    skipped += 1
                    continue
                if allowed_types != {"image", "text", "video"}:
                    msg_type = self._get_content_type(m)
                    if msg_type not in allowed_types:
                        job_repo.record_copied_message(job.id, m.id, None, "skipped", f"content_type:{msg_type}", userbot_id=self._userbot_id)
                        already_done.add(m.id)
                        skipped += 1
                        continue
                if skip_duplicates and dedup_repo.is_duplicate(m, job.destination_id):
                    job_repo.record_copied_message(job.id, m.id, None, "skipped", "duplicate", userbot_id=self._userbot_id)
                    already_done.add(m.id)
                    skipped += 1
                    continue
                to_send.append(m)

            if not to_send:
                job_repo.update_progress(job.id, copied, skipped, failed, checkpoint)
                return False

            async def _send_single(m: Message) -> tuple[str, str | None]:
                """Forward one message; returns (status, reason). Updates src_is_protected."""
                nonlocal src_is_protected
                try:
                    if src_is_protected:
                        await self._send_as_copy(m, dst_entity, copy_text=job.copy_text)
                    else:
                        if job.copy_text:
                            await self._client(ForwardMessagesRequest(
                                from_peer=src_entity,
                                id=[m.id],
                                to_peer=dst_entity,
                                drop_author=True,
                                random_id=[random.randint(0, 2**63 - 1)],  # nosec B311
                            ))
                        else:
                            await self._client.send_file(dst_entity, m.media, caption="")
                    return "copied", None
                except ChatForwardsRestrictedError:
                    src_is_protected = True
                    try:
                        await self._send_as_copy(m, dst_entity, copy_text=job.copy_text)
                        return "copied", None
                    except FloodWaitError:
                        raise
                    except Exception as e:
                        return "failed", str(e)[:200]
                except FloodWaitError:
                    raise
                except Exception as e:
                    return "failed", str(e)[:200]

            if len(to_send) == 1:
                st, reason = await _send_single(to_send[0])
                if st == "copied":
                    copied += 1
                    self._record_transfer(job, to_send[0])
                else:
                    failed += 1
                job_repo.record_copied_message(job.id, to_send[0].id, None, st, reason, userbot_id=self._userbot_id)
                already_done.add(to_send[0].id)
            else:
                # Try fast album send via file refs; fall back to individual forwards (not download)
                album_ok = False
                try:
                    await self._send_group_by_ref(to_send, dst_entity, copy_text=job.copy_text)
                    album_ok = True
                    logger.info(
                        "Job #%d: grouped %d solo media into album (ids=%s)",
                        job.id, len(to_send), [m.id for m in to_send],
                    )
                except FloodWaitError:
                    raise
                except Exception as ref_err:
                    logger.warning(
                        "Job #%d: album ref-send failed (%s) — falling back to %d individual sends",
                        job.id, ref_err, len(to_send),
                    )

                if album_ok:
                    for m in to_send:
                        job_repo.record_copied_message(job.id, m.id, None, "copied", None, userbot_id=self._userbot_id)
                        self._record_transfer(job, m)
                        already_done.add(m.id)
                        copied += 1
                else:
                    # Send only the first message individually (it's the one causing the issue),
                    # then put the rest back into the buffer so they can form a new album.
                    first = to_send[0]
                    st, reason = await _send_single(first)
                    if st == "copied":
                        copied += 1
                        self._record_transfer(job, first)
                    else:
                        failed += 1
                        logger.warning(
                            "Job #%d: failed to send msg #%d individually: %s",
                            job.id, first.id, reason,
                        )
                    job_repo.record_copied_message(job.id, first.id, None, st, reason, userbot_id=self._userbot_id)
                    already_done.add(first.id)

                    # Re-queue the remaining messages for the next album attempt
                    if len(to_send) > 1:
                        remaining = to_send[1:]
                        logger.info(
                            "Job #%d: re-queuing %d messages back to solo buffer after album failure",
                            job.id, len(remaining),
                        )
                        solo_media_buffer = remaining + solo_media_buffer
                        # The re-queued messages are neither sent nor recorded. A
                        # checkpoint past them would make the next run start after
                        # them (_fetch_messages resumes at last_processed_id), so
                        # they would be dropped for good if the job stops here.
                        # Messages are buffered in ascending id order, so every
                        # re-queued id is above the one just sent.
                        checkpoint = first.id

            job_repo.update_progress(job.id, copied, skipped, failed, checkpoint)
            if job_repo.should_stop(job.id):
                logger.info("Job #%d: stop requested (paused/cancelled) after media flush at #%d", job.id, checkpoint)
                return True
            await self._rate_limiter.wait(album=True)
            return False

        async def flush_group() -> bool:
            """Flush pending album group. Returns True if the job must stop (paused/cancelled)."""
            nonlocal copied, skipped, failed, pending_group, current_group_id, src_is_protected
            if not pending_group:
                return False
            if job_repo.should_stop(job.id):
                return True
            group = pending_group
            pending_group = []
            current_group_id = None

            # Send only the members that are not recorded yet. Testing just
            # group[0] and dropping the whole album (as this used to do) lost the
            # remaining items for good whenever a run stopped mid-album: the first
            # item was recorded, so on resume the rest were never sent.
            pending = [m for m in group if m.id not in already_done]
            if not pending:
                return False

            statuses, src_is_protected = await self._process_group(
                job, pending, blocked_words, src_entity, dst_entity, src_is_protected,
                skip_duplicates=skip_duplicates,
            )

            # Every member of `group` is now accounted for — either recorded on an
            # earlier run or recorded just below — so the checkpoint may pass it.
            last_id = group[-1].id
            for msg, (status, skip_reason) in zip(pending, statuses):
                job_repo.record_copied_message(
                    job_id=job.id,
                    source_message_id=msg.id,
                    dest_message_id=None,
                    status=status,
                    skip_reason=skip_reason,
                    userbot_id=self._userbot_id,
                )
                already_done.add(msg.id)
                if status == "copied":
                    copied += 1
                    self._record_transfer(job, msg)
                elif status == "skipped":
                    skipped += 1
                else:
                    failed += 1

            job_repo.update_progress(job.id, copied, skipped, failed, last_id)
            if job_repo.should_stop(job.id):
                logger.info("Job #%d: stop requested (paused/cancelled) after album flush at #%d", job.id, last_id)
                return True
            await self._rate_limiter.wait(album=True)
            return False

        try:
            async for msg in self._fetch_messages(job, src_entity):
                if msg is None or not hasattr(msg, "id"):
                    continue

                if msg.grouped_id:
                    # Existing album: flush solo buffer first, then accumulate
                    if group_media:
                        if await flush_solo_media():
                            return
                    if msg.grouped_id == current_group_id:
                        pending_group.append(msg)
                    else:
                        if await flush_group():
                            return
                        current_group_id = msg.grouped_id
                        pending_group = [msg]
                else:
                    # Individual message: flush any pending album group first
                    if await flush_group():
                        return

                    if group_media and self._is_groupable(msg):
                        # Add to solo buffer (skip if already done)
                        if msg.id not in already_done:
                            solo_media_buffer.append(msg)
                        if len(solo_media_buffer) >= 10:
                            if await flush_solo_media():
                                return
                    else:
                        # Non-groupable: flush solo buffer, then process normally
                        if group_media:
                            if await flush_solo_media():
                                return

                        if msg.id in already_done:
                            continue

                        status, skip_reason, src_is_protected = await self._process_message(
                            job, msg, blocked_words, src_entity, dst_entity, src_is_protected,
                            skip_duplicates=skip_duplicates,
                        )

                        job_repo.record_copied_message(
                            job_id=job.id,
                            source_message_id=msg.id,
                            dest_message_id=None,
                            status=status,
                            skip_reason=skip_reason,
                            userbot_id=self._userbot_id,
                        )
                        already_done.add(msg.id)

                        if status == "copied":
                            copied += 1
                            self._record_transfer(job, msg)
                        elif status == "skipped":
                            skipped += 1
                        else:
                            failed += 1

                        job_repo.update_progress(job.id, copied, skipped, failed, msg.id)
                        if copied - _last_progress_log >= 50:
                            _last_progress_log = copied
                            logger.info(
                                "Job #%d progress: copied=%d skipped=%d failed=%d last_id=#%d",
                                job.id, copied, skipped, failed, msg.id,
                            )
                        # Checked every message, not every 25: it is one indexed
                        # primary-key lookup, which is nothing next to the 2–5s
                        # rate-limiter sleep between sends — and it makes cancel
                        # take effect on the next message instead of 25 later.
                        if job_repo.should_stop(job.id):
                            logger.info("Job #%d: stop requested (paused/cancelled) — stopping at msg #%d", job.id, msg.id)
                            job_repo.update_progress(job.id, copied, skipped, failed, msg.id)
                            return

                        _msgs_since_pause_check += 1
                        if _msgs_since_pause_check >= 25:
                            _msgs_since_pause_check = 0
                            if self._resolve_callback:
                                await self._resolve_callback()

                        _msgs_since_limit_check += 1
                        if _msgs_since_limit_check >= 100 and self._userbot_id is not None:
                            _msgs_since_limit_check = 0
                            from app.ui.texts import DAILY_LIMIT
                            # Per-account, matching the pre-job check: one capped
                            # account must not stop the others.
                            count_today = job_repo.get_daily_count_for_userbot(self._userbot_id)
                            if count_today >= DAILY_LIMIT:
                                logger.warning(
                                    "Job #%d: userbot #%d hit its daily limit mid-job (%d msgs) — pausing at msg #%d",
                                    job.id, self._userbot_id, count_today, msg.id,
                                )
                                job_repo.update_progress(job.id, copied, skipped, failed, msg.id)
                                job_repo.pause_job(job.id)
                                return

                        await self._rate_limiter.wait()

            # Flush any remaining buffers at end of stream
            if await flush_group():
                return
            if group_media:
                while solo_media_buffer:
                    if await flush_solo_media():
                        return

        except FloodWaitError:
            logger.warning("Job #%d: FloodWait encountered", job.id)
            job_repo.update_progress(job.id, copied, skipped, failed, job.last_processed_id or 0)
            raise

        except (ChatWriteForbiddenError, ChannelPrivateError) as e:
            # Access lost mid-job. Progress is checkpointed, so another userbot
            # can pick this job up and resume from where this one stopped.
            job_repo.update_progress(job.id, copied, skipped, failed, job.last_processed_id or 0)
            logger.warning(
                "Job #%d: userbot %s lost access mid-job (%s) — requesting reassignment",
                job.id, self._userbot_id, e,
            )
            raise NoAccessError(f"אין הרשאת גישה/כתיבה לערוץ: {e}") from e

        except Exception as e:
            logger.exception("Job #%d: unexpected error: %s", job.id, e)
            raise

        # The source can run out at the same moment the user cancels. Never write a
        # terminal state over 'cancelled' — that silently undid the cancel and left
        # the job looking like it had completed normally.
        if job_repo.should_stop(job.id):
            logger.info(
                "Job #%d: stopped by user at end of run (copied=%d skipped=%d failed=%d)",
                job.id, copied, skipped, failed,
            )
            return

        if job.continuous:
            # A continuous job doesn't finish — it graduates from copying history
            # to listening for new messages. The worker picks it up as a listener
            # on its next reconcile.
            job_repo.mark_backfill_done(job.id)
            logger.info(
                "Job #%d: history copy finished (copied=%d skipped=%d failed=%d) "
                "— switching to live listening",
                job.id, copied, skipped, failed,
            )
        else:
            job_repo.mark_completed(job.id)
            logger.info(
                "Job #%d completed: copied=%d skipped=%d failed=%d",
                job.id, copied, skipped, failed,
            )

        # Generate Telegraph report for notable (failed / unexpected-skipped) messages
        report_msgs = job_repo.get_report_messages(job.id)
        if not report_msgs:
            logger.info("Job #%d: no notable messages — Telegraph report skipped", job.id)
        if report_msgs:
            from app.services import telegraph_service
            url = await telegraph_service.create_report(
                job.id, report_msgs, src_rec.resolved_id, src_rec.channel_ref
            )
            if url:
                job_repo.save_report_url(job.id, url)
                logger.info("Job #%d Telegraph report: %s", job.id, url)

    # ── Message fetching ───────────────────────────────────────────────────────

    async def _fetch_messages(
        self, job: Job, src_entity
    ) -> AsyncIterator[Message]:
        """Yield messages in ascending ID order (oldest first) for safe resume."""
        client = self._client
        min_id = job.last_processed_id or 0

        if job.mode == "all":
            async for msg in client.iter_messages(src_entity, reverse=True, min_id=min_id):
                yield msg

        elif job.mode == "id_range":
            id_from = max(job.id_from or 1, min_id + 1)
            id_to = job.id_to or 0
            async for msg in client.iter_messages(
                src_entity, reverse=True, min_id=id_from - 1, max_id=id_to + 1
            ):
                if id_from <= msg.id <= id_to:
                    yield msg

        elif job.mode == "date_range":
            # Both sides are timezone-aware: the bounds are Israel local time (what
            # the user typed) and msg.date is UTC. Stripping the tzinfo and
            # comparing them as naive datetimes shifted the whole range by the UTC
            # offset — 2h, or 3h under DST.
            date_from = _parse_date(job.date_from)
            date_to = _parse_date(job.date_to)
            async for msg in client.iter_messages(src_entity, reverse=True, min_id=min_id):
                if not msg.date:
                    continue
                msg_date = _as_aware_utc(msg.date)
                if date_from and msg_date < date_from:
                    continue
                if date_to and msg_date > date_to:
                    break
                yield msg

        elif job.mode == "single_id":
            if job.single_message_id and job.single_message_id > min_id:
                msg = await client.get_messages(src_entity, ids=job.single_message_id)
                if msg:
                    yield msg

    # ── Message processing ─────────────────────────────────────────────────────

    async def _process_group(
        self,
        job: Job,
        group: list[Message],
        blocked_words: list[str],
        src_entity,
        dst_entity,
        src_is_protected: bool,
        skip_duplicates: bool = False,
    ) -> tuple[list[tuple[str, Optional[str]]], bool]:
        """
        Forward a media-group (album) as a single batch.
        Returns (statuses, src_is_protected) — one status per message.
        src_is_protected is updated to True if the channel turns out to be protected.
        """
        # Global block word checks
        if blocked_words and any(self._is_blocked(m, blocked_words) for m in group):
            logger.debug("Job #%d: group %d blocked by filter", job.id, group[0].grouped_id)
            return [("skipped", "blocked_word")] * len(group), src_is_protected

        allowed_types: set[str] = set((job.content_types or "text,image,video").split(","))

        final_statuses: list[tuple[str, Optional[str]]] = []
        send_group: list[Message] = []

        # Filter items individually
        for m in group:
            if not job.copy_text and (not m.media or isinstance(m.media, MessageMediaUnsupported)):
                final_statuses.append(("skipped", "text_stripped_empty"))
                continue
            
            if allowed_types != {"image", "text", "video"}:
                msg_type = self._get_content_type(m)
                if msg_type not in allowed_types:
                    final_statuses.append(("skipped", f"content_type:{msg_type}"))
                    continue

            if skip_duplicates and dedup_repo.is_duplicate(m, job.destination_id):
                final_statuses.append(("skipped", "duplicate"))
                continue

            final_statuses.append(None) # placeholder
            send_group.append(m)

        if not send_group:
            logger.debug("Job #%d: album group=%s all items skipped", job.id, group[0].grouped_id)
            return [st for st in final_statuses if st is not None], src_is_protected

        def fill_statuses(st_tuple):
            return [st_tuple if st is None else st for st in final_statuses]

        if len(send_group) == 1:
            st, reason, src_is_protected = await self._process_message(
                job, send_group[0], [], src_entity, dst_entity, src_is_protected
            )
            return fill_statuses((st, reason)), src_is_protected

        if src_is_protected:
            # Channel already known to be protected — skip straight to download+upload
            try:
                await self._send_group_as_copy(send_group, dst_entity, copy_text=job.copy_text)
                return fill_statuses(("copied", None)), src_is_protected
            except FloodWaitError:
                raise
            except Exception as e:
                logger.warning("Job #%d: download+upload album failed: %s", job.id, e)
                return fill_statuses(("failed", str(e)[:200])), src_is_protected

        ids = [m.id for m in send_group]
        try:
            if job.copy_text:
                await self._client(ForwardMessagesRequest(
                    from_peer=src_entity,
                    id=ids,
                    to_peer=dst_entity,
                    drop_author=True,
                    random_id=[random.randint(0, 2**63 - 1) for _ in ids],  # nosec B311
                ))
            else:
                # Via _send_group_as_copy, not _send_group_by_ref directly: it tries
                # the file references first and falls back to download+reupload for
                # items the album API cannot carry (plain docs, GIFs, round notes).
                await self._send_group_as_copy(send_group, dst_entity, copy_text=False)
            logger.info(
                "Job #%d: forwarded album of %d items (ids=%s)",
                job.id, len(ids), ids,
            )
            return fill_statuses(("copied", None)), src_is_protected

        except ChatForwardsRestrictedError:
            src_is_protected = True
            logger.info(
                "Job #%d: source channel is protected — switching to download+upload for all remaining messages",
                job.id,
            )
            try:
                await self._send_group_as_copy(send_group, dst_entity, copy_text=job.copy_text)
                return fill_statuses(("copied", None)), src_is_protected
            except FloodWaitError:
                raise
            except Exception as e:
                logger.warning("Job #%d: download+upload album failed: %s", job.id, e)
                return fill_statuses(("failed", str(e)[:200])), src_is_protected

        except FloodWaitError:
            raise

        except Exception as e:
            logger.warning(
                "Job #%d: failed to forward album (ids=%s): %s",
                job.id, ids, e,
            )
            return fill_statuses(("failed", str(e)[:200])), src_is_protected

    async def _process_message(
        self,
        job: Job,
        msg: Message,
        blocked_words: list[str],
        src_entity,
        dst_entity,
        src_is_protected: bool,
        skip_duplicates: bool = False,
    ) -> tuple[str, Optional[str], bool]:
        """Copy one message. Returns (status, skip_reason, src_is_protected)."""

        # Filter check
        if blocked_words and self._is_blocked(msg, blocked_words):
            logger.debug("Job #%d: msg #%d blocked by filter", job.id, msg.id)
            return "skipped", "blocked_word", src_is_protected

        # Already sent this exact content to this destination
        if skip_duplicates and dedup_repo.is_duplicate(msg, job.destination_id):
            logger.debug("Job #%d: msg #%d skipped as duplicate", job.id, msg.id)
            return "skipped", "duplicate", src_is_protected

        # Content type filter
        allowed_types: set[str] = set((job.content_types or "text,image,video").split(","))
        if allowed_types != {"image", "text", "video"}:
            msg_type = self._get_content_type(msg)
            if msg_type not in allowed_types:
                logger.debug("Job #%d: msg #%d skipped (type=%s not in %s)", job.id, msg.id, msg_type, allowed_types)
                return "skipped", f"content_type:{msg_type}", src_is_protected

        # Supported type check
        if not self._is_supported_type(msg):
            logger.debug("Job #%d: msg #%d unsupported type", job.id, msg.id)
            return "skipped", "unsupported_type", src_is_protected

        # Skip empty service messages
        if not msg.text and not msg.media:
            return "skipped", "empty_message", src_is_protected

        if not job.copy_text and (not msg.media or isinstance(msg.media, MessageMediaUnsupported)):
            return "skipped", "text_stripped_empty", src_is_protected

        if src_is_protected:
            # Channel already known to be protected — skip straight to download+upload
            try:
                await self._send_as_copy(msg, dst_entity, copy_text=job.copy_text)
                return "copied", None, src_is_protected
            except FloodWaitError:
                raise
            except Exception as e:
                logger.warning("Job #%d: failed to copy msg #%d: %s", job.id, msg.id, e)
                return "failed", str(e)[:200], src_is_protected

        try:
            if job.copy_text:
                await self._client(ForwardMessagesRequest(
                    from_peer=src_entity,
                    id=[msg.id],
                    to_peer=dst_entity,
                    drop_author=True,
                    random_id=[random.randint(0, 2**63 - 1)],  # nosec B311
                ))
            else:
                await self._client.send_file(dst_entity, msg.media, caption="")
            return "copied", None, src_is_protected

        except ChatForwardsRestrictedError:
            src_is_protected = True
            logger.info(
                "Job #%d: source channel is protected — switching to download+upload for all remaining messages",
                job.id,
            )
            try:
                await self._send_as_copy(msg, dst_entity, copy_text=job.copy_text)
                return "copied", None, src_is_protected
            except FloodWaitError:
                raise
            except Exception as e:
                logger.warning("Job #%d: failed to copy msg #%d: %s", job.id, msg.id, e)
                return "failed", str(e)[:200], src_is_protected

        except FloodWaitError:
            raise

        except Exception as e:
            logger.warning("Job #%d: failed to copy msg #%d: %s", job.id, msg.id, e)
            return "failed", str(e)[:200], src_is_protected

    def _record_transfer(self, job: Job, msg: Message) -> None:
        """Add a successfully transferred message to the global dedup registry."""
        dedup_repo.record_message(
            msg,
            destination_id=job.destination_id,
            source_id=job.source_id,
            job_id=job.id,
        )

    # ── Continuous sync ────────────────────────────────────────────────────────

    async def handle_live_message(self, job: Job, msg: Message) -> str:
        """
        Copy a single message that just arrived in the source channel.

        Used by continuous ("always listening") jobs. Runs the same filters,
        dedup and rate limiting as a bulk job, and keeps the same per-job
        counters so the UI reports live progress identically.
        Returns the recorded status: copied | skipped | failed.
        """
        from app.repositories import state_repo

        settings = state_repo.get_settings_dict()
        self._rate_limiter.update_from_settings(settings)
        skip_duplicates = settings.get("skip_duplicates", "0") == "1"

        if msg is None or not hasattr(msg, "id"):
            return "skipped"

        # Never process the same source message twice.
        if job_repo.is_message_processed(job.id, msg.id):
            return "skipped"

        blocked_words: list[str] = filter_repo.get_word_strings() if job.use_blocked_words else []

        src_rec = source_repo.get_source_by_id(job.source_id)
        dst_rec = source_repo.get_destination_by_id(job.destination_id)
        if not src_rec or not dst_rec:
            return "failed"

        from app.worker.telegram_utils import get_entity_safe
        try:
            src_entity = await get_entity_safe(
                self._client, str(src_rec.resolved_id or src_rec.channel_ref)
            )
            dst_entity = await get_entity_safe(
                self._client, str(dst_rec.resolved_id or dst_rec.channel_ref)
            )
        except (ChannelPrivateError, ValueError) as e:
            raise NoAccessError(f"אין גישה לערוץ: {e}") from e

        status, skip_reason, _ = await self._process_message(
            job, msg, blocked_words, src_entity, dst_entity, False,
            skip_duplicates=skip_duplicates,
        )

        job_repo.record_copied_message(job.id, msg.id, None, status, skip_reason, userbot_id=self._userbot_id)

        fresh = job_repo.get_by_id(job.id) or job
        copied = fresh.copied_count + (1 if status == "copied" else 0)
        skipped = fresh.skipped_count + (1 if status == "skipped" else 0)
        failed = fresh.failed_count + (1 if status == "failed" else 0)
        job_repo.update_progress(job.id, copied, skipped, failed, msg.id)

        if status == "copied":
            self._record_transfer(job, msg)
            await self._rate_limiter.wait()

        logger.info(
            "Job #%d (continuous): live msg #%d → %s%s",
            job.id, msg.id, status, f" ({skip_reason})" if skip_reason else "",
        )
        return status

    async def _forward_without_credit(
        self, msg: Message, src_entity, dst_entity
    ) -> None:
        """Forward a single message without attribution (only used externally)."""
        await self._client(ForwardMessagesRequest(
            from_peer=src_entity,
            id=[msg.id],
            to_peer=dst_entity,
            drop_author=True,
            random_id=[random.randint(0, 2**63 - 1)],  # nosec B311
        ))

    async def _send_as_copy(self, msg: Message, dst_entity, copy_text: bool = True) -> None:
        """Download and re-upload a single message (used when forwarding is blocked).
        Raises RuntimeError if media download returns None (caller records as failed)."""
        text = msg.text if copy_text else ""

        if not msg.media or isinstance(msg.media, MessageMediaUnsupported):
            if text:
                await self._client.send_message(dst_entity, text)
            return

        file_bytes: Optional[bytes] = await self._client.download_media(msg, file=bytes)
        if file_bytes is None:
            # Media could not be downloaded (e.g. forwarded from protected channel)
            raise RuntimeError("download_failed: media returned None (protected or unavailable)")

        await self._client.send_file(dst_entity, file_bytes, caption=text or None)

    async def _send_group_by_ref(self, group: list[Message], dst_entity, copy_text: bool = True) -> None:
        """
        Send a media album using existing Telegram file references — no download needed.

        All-or-nothing: raises RuntimeError if any message in the group cannot be
        represented as album media, without sending anything. Callers record a
        whole group with one status, so a partial send here would mark messages
        as copied that never left the source. Raising instead lets the caller fall
        back to download+reupload (_send_group_as_copy) or to individual sends.
        """
        from telethon.tl.functions.messages import SendMultiMediaRequest
        from telethon.tl.types import (
            InputSingleMedia, InputMediaPhoto, InputMediaDocument,
            InputPhoto, InputDocument,
        )

        multi: list = []
        unsupported: list[int] = []
        for m in group:
            if not m.media or isinstance(m.media, MessageMediaUnsupported):
                unsupported.append(m.id)
                continue
            type_name = m.media.__class__.__name__
            caption = m.text or ""
            if type_name == "MessageMediaPhoto":
                p = m.media.photo
                input_media = InputMediaPhoto(
                    id=InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference)
                )
            elif type_name == "MessageMediaDocument":
                d = m.media.document
                if not d:
                    unsupported.append(m.id)
                    continue
                # Only regular videos in albums — GIFs, round notes and plain docs
                # cause MEDIA_INVALID in SendMultiMediaRequest.
                is_regular_video = any(
                    attr.__class__.__name__ == "DocumentAttributeVideo"
                    and not getattr(attr, "round_message", False)
                    for attr in d.attributes
                )
                if not is_regular_video:
                    unsupported.append(m.id)
                    continue
                input_media = InputMediaDocument(
                    id=InputDocument(id=d.id, access_hash=d.access_hash, file_reference=d.file_reference)
                )
            else:
                unsupported.append(m.id)
                continue
            multi.append(InputSingleMedia(
                media=input_media,
                random_id=random.randint(0, 2**63 - 1),  # nosec B311
                message=caption if copy_text else "",
            ))

        if unsupported or not multi:
            raise RuntimeError(
                f"album_ref_unsupported: {len(unsupported)} of {len(group)} item(s) "
                f"cannot be sent as album media (ids={unsupported})"
            )

        await self._client(SendMultiMediaRequest(peer=dst_entity, multi_media=multi))

    async def _send_group_as_copy(self, group: list[Message], dst_entity, copy_text: bool = True) -> None:
        """
        Send a media group by trying file references first (fast), then
        falling back to download+reupload (slow, used when refs are expired).
        """
        try:
            await self._send_group_by_ref(group, dst_entity, copy_text=copy_text)
            return
        except FloodWaitError:
            raise
        except Exception as e:
            logger.warning("Job: send_group_by_ref failed (%s) — falling back to download+upload", e)

        # Fallback: download and re-upload
        files: list[bytes] = []
        captions: list[str] = []
        failed_downloads: list[Message] = []
        for m in group:
            if m.media and not isinstance(m.media, MessageMediaUnsupported):
                data: Optional[bytes] = await self._client.download_media(m, file=bytes)
                if data:
                    files.append(data)
                    captions.append(m.text if copy_text else "")
                else:
                    failed_downloads.append(m)
            # text-only messages in a group are included via caption, no separate download needed

        if failed_downloads:
            # Raise so callers can record these as failed instead of silently dropping
            ids = [m.id for m in failed_downloads]
            raise RuntimeError(
                f"download_failed: {len(failed_downloads)} media item(s) returned None (ids={ids})"
            )

        if not files:
            text = next((m.text for m in group if m.text), None) if copy_text else None
            if text:
                await self._client.send_message(dst_entity, text)
            return

        await self._client.send_file(dst_entity, files, caption=captions)

    def _is_blocked(self, msg: Message, blocked_words: list[str]) -> bool:
        text = (msg.text or "").lower()
        return any(word in text for word in blocked_words)

    @staticmethod
    def _is_groupable(msg: Message) -> bool:
        """True if this message can be added to a Telegram media album.
        Only photos and regular videos — NOT GIFs/animations or round-video notes,
        which cause MEDIA_INVALID in SendMultiMediaRequest."""
        if not msg.media or isinstance(msg.media, MessageMediaUnsupported):
            return False
        type_name = msg.media.__class__.__name__
        if type_name == "MessageMediaPhoto":
            return True
        if type_name == "MessageMediaDocument":
            doc = msg.media.document
            if doc:
                for attr in doc.attributes:
                    if attr.__class__.__name__ == "DocumentAttributeVideo":
                        # Exclude round-video notes (video_note=True / round_message=True)
                        if not getattr(attr, "round_message", False):
                            return True
        return False

    @staticmethod
    def _get_content_type(msg: Message) -> str:
        """Classify message as 'text', 'image', 'video', or 'other'."""
        if not msg.media or isinstance(msg.media, MessageMediaUnsupported):
            return "text"
        type_name = msg.media.__class__.__name__
        if type_name == "MessageMediaPhoto":
            return "image"
        if type_name == "MessageMediaDocument":
            doc = msg.media.document
            if doc:
                for attr in doc.attributes:
                    cls = attr.__class__.__name__
                    if cls == "DocumentAttributeSticker":
                        return "image"
                    if cls in ("DocumentAttributeVideo", "DocumentAttributeAnimated"):
                        return "video"
        return "other"

    def _is_supported_type(self, msg: Message) -> bool:
        if not msg.media:
            return True
        if isinstance(msg.media, MessageMediaUnsupported):
            return False
        type_name = msg.media.__class__.__name__
        if any(t in type_name for t in ("Poll", "Game", "Invoice", "GeoLive")):
            return False
        return True


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """
    Parse a job's stored date bound.

    The value is whatever the user typed into the wizard, i.e. Israel local time.
    The result is timezone-aware so it can be compared directly with the UTC dates
    Telethon puts on messages. ZoneInfo resolves the offset per wall-clock date,
    so the DST boundary is handled.
    """
    if not date_str:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=_IL_TZ)
        except ValueError:
            continue
    return None


def _as_aware_utc(dt: datetime) -> datetime:
    """Telethon dates are aware UTC; tolerate a naive one rather than crash on it."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
