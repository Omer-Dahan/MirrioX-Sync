"""
Core copy logic using Telethon. Executes a single job end-to-end.

A job runs on one account by default: a single ascending pass, which is what
keeps the destination in source order. When two or more accounts can reach both
of the job's channels, the account that claimed the job becomes its *leader* and
splits the source ID range into chunks (job_chunks). Every free account then
claims chunks of its own, so one job is copied by several accounts at once. Past
the split the leader is just another worker: whichever account closes the last
chunk owns the job's terminal state and report, so nobody sits idle waiting for
the stragglers.
"""
# pylint: disable=too-many-branches,too-many-statements,too-many-locals
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import shutil
import tempfile
import time
from dataclasses import dataclass, field
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

from app.models import ALL_CONTENT_TYPES, DEFAULT_CONTENT_TYPES, Job, JobChunk, NoAccessError
from app.repositories import job_repo, job_chunk_repo, filter_repo, source_repo, dedup_repo
from app.worker.rate_limiter import LabeledAdapter, RateLimiter

logger = logging.getLogger(__name__)

# Job date bounds are entered in Israel local time, matching the rest of the app
# (daily limits, transfer stats).
_IL_TZ = ZoneInfo("Asia/Jerusalem")

# Sharding shape. The plan aims for a fixed number of chunks rather than a fixed
# chunk size, so a 500-message channel and a 500,000-message one both end up with
# enough chunks to keep every account busy without producing thousands of rows.
_TARGET_CHUNKS = 200
_MIN_CHUNK_IDS = 200
# A chunk whose owner has been silent this long is assumed dead and handed back.
# Generous on purpose — see job_chunk_repo.reclaim_stale.
_CHUNK_STALE_S = 30 * 60
# Telegram's caption limit for a media message. A longer caption is rejected with
# MEDIA_CAPTION_TOO_LONG, and in an album that one message takes the whole batch
# down with it — so an over-long caption keeps a message out of album grouping.
_MAX_CAPTION_LEN = 1024
# How often the retry pass re-reads this account's daily count. The pass is capped
# at a few hundred messages, so this is frequent enough to stop within a handful of
# sends of the limit without querying once per message.
_RETRY_CAP_CHECK_EVERY = 25
# How many times one send may be re-attempted after a short FloodWait before the
# error is allowed out to the runner. A handful of consecutive waits on the same
# message means the account is genuinely throttled, not momentarily unlucky.
_FLOOD_INLINE_MAX_ATTEMPTS = 3
# Messages copied without trouble before the job's retry counter is cleared. The
# counter exists to catch a job that cannot make progress; a job that has just
# copied this many in a row plainly can.
_RETRY_RESET_AFTER_COPIES = 200
# Where the emergency download+re-upload path stages files. Only reached when the
# operator turns `allow_download_upload` on and a by-reference send was refused.
_TEMP_MEDIA_DIR = os.path.join(tempfile.gettempdir(), "mirriox_media")
# How long a cached read of app_settings is good for on the per-message paths
# (hyper backup). Bulk jobs re-read once per job and never use this.
_SETTINGS_CACHE_TTL_S = 10.0


@dataclass
class _SourceProtection:
    """
    What this run has learned about the source channel's forward restriction.

    Shared by reference, not copied: `_flood_retry` re-invokes the send it wraps,
    and passing a plain bool meant every inner attempt started over from a
    ForwardMessagesRequest Telegram had already refused once — a wasted call, and
    a wasted download+re-upload behind it, for every attempt of every message.
    """
    # True once Telegram refuses to forward out of the source, or once the source
    # row already says the channel blocks forwarding.
    is_protected: bool = False
    # None until the first protected message of the run answers it: does sending
    # by file_reference alone work on this pair of channels? Learned once so no
    # message pays for a doomed attempt, and never un-learned after a success —
    # one odd message must not push the whole job onto the slow path.
    ref_send_works: Optional[bool] = None
    # True once this run has told the admin that it cannot copy at all (the fast
    # path was refused and the download fallback is off). Without it a job of ten
    # thousand messages would send ten thousand identical alerts.
    blocked_reported: bool = False


@dataclass
class _JobContext:
    """Everything a copy pass needs that is resolved once per job, not per chunk."""
    src_rec: object
    src_entity: object
    # (destination_id, resolved entity) per destination — one entry for a classic
    # single-destination job, several for random fan-out.
    dst_targets: list
    blocked_words: list[str]
    skip_duplicates: bool
    protection: _SourceProtection = field(default_factory=_SourceProtection)


class _Progress:
    """
    A single pass's counters, flushed to the DB as deltas.

    Deltas, not totals: a sharded job has several accounts copying different
    chunks at the same time, and each one only knows its own tally. Writing
    absolute counts would make every flush overwrite the other accounts' work.
    """

    def __init__(self, job_id: int, chunk_id: Optional[int]) -> None:
        self._job_id = job_id
        self._chunk_id = chunk_id
        self.copied = 0
        self.skipped = 0
        self.failed = 0
        self._flushed = (0, 0, 0)

    def flush(self, checkpoint: int) -> None:
        delta = (
            self.copied - self._flushed[0],
            self.skipped - self._flushed[1],
            self.failed - self._flushed[2],
        )
        self._flushed = (self.copied, self.skipped, self.failed)
        if self._chunk_id is None:
            job_repo.add_progress(self._job_id, *delta, last_processed_id=checkpoint)
        else:
            # The job-wide checkpoint means nothing once several accounts are
            # copying different parts of the range — the chunk carries its own.
            job_repo.add_progress(self._job_id, *delta)
            job_chunk_repo.checkpoint(self._chunk_id, checkpoint)


class CopyEngine:
    """Executes a copy job using the provided Telethon client."""

    def __init__(
        self,
        client: TelegramClient,
        resolve_callback: Optional[Callable[[], Awaitable[None]]] = None,
        userbot_id: Optional[int] = None,
        label: Optional[str] = None,
    ) -> None:
        self._client = client
        self._rate_limiter = RateLimiter(label=label)
        self._resolve_callback = resolve_callback
        self._userbot_id = userbot_id
        self._log = LabeledAdapter(logger, {"label": label}) if label else logger
        # Refreshed from settings on every job; see _load_copy_settings.
        self._flood_inline_max_s = 60
        # Off unless the operator turns it on: a protected source is copied by
        # file reference, and download+re-upload is the rare emergency route.
        self._allow_download_upload = False
        self._max_download_mb = 2048
        self._settings_loaded_at = 0.0

    def _load_copy_settings(self, settings: dict[str, str]) -> None:
        """Refresh the per-run knobs every entry point needs. Called once per job."""
        from app.ui.texts import toggle_is_on

        self._rate_limiter.update_from_settings(settings)
        self._flood_inline_max_s = _int_setting(settings, "flood_inline_max_s", 60)
        self._allow_download_upload = toggle_is_on(settings, "allow_download_upload")
        self._max_download_mb = _int_setting(settings, "max_download_mb", 2048)
        self._settings_loaded_at = time.monotonic()

    def _load_copy_settings_cached(self) -> None:
        """
        Same refresh, but at most once every `_SETTINGS_CACHE_TTL_S`.

        For the paths that run per message rather than per job (hyper backup):
        re-reading app_settings on every single message put three sqlite queries
        on the shared event loop for each one, and none of these knobs changes
        often enough to be worth that.
        """
        if time.monotonic() - self._settings_loaded_at < _SETTINGS_CACHE_TTL_S:
            return
        from app.repositories import state_repo
        self._load_copy_settings(state_repo.get_settings_dict())

    # ── FloodWait handling ─────────────────────────────────────────────────────

    async def _flood_retry(self, op: Callable[[], Awaitable], what: str):
        """
        Run one send operation, sleeping through short FloodWaits in place.

        Telethon is configured with flood_sleep_threshold=0 so every FloodWait
        reaches us, and every one of them used to unwind the whole copy pass: a
        five-second wait cost the job its place in the queue and one of its
        retries. Anything Telegram says it can forgive within
        `flood_inline_max_s` is simply waited out and re-attempted from the same
        point instead. Longer waits — and a message that keeps flooding — still
        go up to the runner, which is where "this account needs to stop" belongs.

        Safe to re-run: every send this wraps is a single all-or-nothing request,
        so a FloodWait means nothing reached the destination.
        """
        attempts = 0
        while True:
            try:
                return await op()
            except FloodWaitError as e:
                attempts += 1
                if e.seconds > self._flood_inline_max_s or attempts > _FLOOD_INLINE_MAX_ATTEMPTS:
                    self._rate_limiter.note_flood_wait(e.seconds)
                    raise
                self._log.warning(
                    "%s: FloodWait %ds — waiting in place (attempt %d/%d)",
                    what, e.seconds, attempts, _FLOOD_INLINE_MAX_ATTEMPTS,
                )
                await self._rate_limiter.handle_flood_wait(e.seconds)

    async def note_flood_wait(self, seconds: int, sleep: bool = True) -> None:
        """Feed a FloodWait the runner caught back into this account's pacing."""
        if sleep:
            await self._rate_limiter.handle_flood_wait(seconds)
        else:
            self._rate_limiter.note_flood_wait(seconds)

    # ── Entry points ───────────────────────────────────────────────────────────

    async def run_job(self, job: Job) -> bool:
        """
        Run a job as its leader — the account that claimed it out of the queue.

        With one eligible account this is the same single ordered pass it always
        was. With two or more the job is sharded and this account works chunks
        alongside the others, then closes the job once the last chunk is done.

        Returns True only if this call is the one that closed the job — the
        caller uses that to decide whether to announce it.
        """
        ctx = await self._prepare(job)
        if ctx is None:
            return False

        job_repo.mark_started(job.id)

        if not await self._plan_shards(job, ctx):
            outcome = await self._copy_stream(job, ctx, chunk=None)
            if outcome == "capped":
                job_repo.release_job(job.id, "pending")
            if outcome != "completed":
                return False
            return await self._finalize(job, ctx)

        return await self._lead_sharded(job, ctx)

    async def run_chunk(self, job: Job, chunk: JobChunk) -> bool:
        """
        Copy one chunk of a sharded job.

        A sharded job has no account standing by to close it: the one that copies
        its last chunk does that too, whichever account that turns out to be. The
        terminal write inside _finalize is atomic, so two accounts finishing at
        the same moment still close the job exactly once.

        Returns True only if this call is the one that closed the job.
        """
        ctx = await self._prepare(job)
        if ctx is None:
            return False
        try:
            outcome = await self._copy_stream(job, ctx, chunk=chunk)
        except Exception:
            job_chunk_repo.release(chunk.id, self._userbot_id)
            raise
        if outcome != "completed":
            job_chunk_repo.release(chunk.id, self._userbot_id)
            return False

        job_chunk_repo.mark_done(chunk.id, self._userbot_id)
        if job_chunk_repo.count_unfinished(job.id) == 0:
            return await self._finalize(job, ctx)
        return False

    # ── Leading a sharded job ──────────────────────────────────────────────────

    async def _lead_sharded(self, job: Job, ctx: _JobContext) -> bool:
        """True only if this account ended up being the one that closed the job."""
        while True:
            job_chunk_repo.reclaim_stale(job.id, _CHUNK_STALE_S)
            chunk = job_chunk_repo.claim_next(job.id, self._userbot_id)

            if chunk is not None:
                try:
                    outcome = await self._copy_stream(job, ctx, chunk=chunk)
                except Exception:
                    job_chunk_repo.release(chunk.id, self._userbot_id)
                    raise
                if outcome == "completed":
                    job_chunk_repo.mark_done(chunk.id, self._userbot_id)
                    continue
                job_chunk_repo.release(chunk.id, self._userbot_id)
                if outcome == "capped":
                    # Out of quota for the day. Hand the whole job back so an
                    # account with budget leads it — finished chunks stay done, so
                    # nothing gets copied twice.
                    job_repo.release_job(job.id, "pending")
                return False

            # Nothing left to claim. Whoever closes the last chunk finalises the
            # job — including this account, if it happens to be the one. Standing
            # by for the stragglers instead used to idle a whole account for as
            # long as the slowest chunk took.
            if job_chunk_repo.count_unfinished(job.id) == 0:
                return await self._finalize(job, ctx)
            return False

    async def _plan_shards(self, job: Job, ctx: _JobContext) -> bool:
        """
        Split the job's remaining ID range into chunks. True once it is sharded.

        Sharding needs at least two accounts known to reach both channels: with
        one there is nothing to parallelise, and a single ascending pass is what
        keeps the destination in source order. A job planned by an earlier run
        stays sharded — its finished chunks are exactly the work not to redo.
        """
        if job_chunk_repo.count_for_job(job.id) > 0:
            return True
        if job.mode == "single_id":
            return False

        eligible = self._eligible_accounts(job)
        if len(eligible) < 2:
            return False

        bounds = await self._shard_bounds(job, ctx)
        if bounds is None:
            return False
        lo, hi = bounds

        size = max(_MIN_CHUNK_IDS, math.ceil((hi - lo + 1) / _TARGET_CHUNKS))
        ranges = [(start, min(start + size - 1, hi)) for start in range(lo, hi + 1, size)]
        if len(ranges) < 2:
            return False  # too small to be worth splitting

        job_chunk_repo.plan(job.id, ranges)
        self._log.info(
            "Job #%d: %d accounts can reach both channels — sharded ids %d–%d into "
            "%d chunk(s) of ~%d ids each",
            job.id, len(eligible), lo, hi, len(ranges), size,
        )
        return True

    def _eligible_accounts(self, job: Job) -> set[int]:
        """Active accounts that can actually run this job, right now."""
        from app.repositories import channel_access_repo, userbot_repo

        active = {u.id for u in userbot_repo.get_active()}
        with_access = channel_access_repo.active_with_access_all(
            job.source_id, job.destination_id_list()
        )
        # This account resolved both channels a moment ago, so it plainly has
        # access even if its own probe hasn't been recorded yet.
        if self._userbot_id is not None:
            with_access.add(self._userbot_id)
        eligible = (active & with_access) - job.excluded_ids()
        # A user-chosen allow-list caps who may run the job. Intersecting here
        # keeps a job restricted to one account from being sharded across several.
        allowed = job.allowed_ids()
        if allowed:
            eligible &= allowed
        return eligible

    async def _shard_bounds(self, job: Job, ctx: _JobContext) -> Optional[tuple[int, int]]:
        """The lowest and highest source message ID this job still has to cover."""
        resume_from = (job.last_processed_id or 0) + 1
        if job.mode == "id_range":
            lo = max(job.id_from or 1, resume_from)
            hi = job.id_to or 0
        else:
            # 'all' and 'date_range' are both bounded by the channel itself. The
            # date filter stays inside the copy loop, so a chunk that falls outside
            # the requested dates simply yields nothing.
            lo = max(1, resume_from)
            msgs = await self._client.get_messages(ctx.src_entity, limit=1)
            hi = msgs[0].id if msgs else 0
        if hi <= lo:
            return None
        return lo, hi

    # ── Shared setup / teardown ────────────────────────────────────────────────

    async def _prepare(self, job: Job) -> Optional[_JobContext]:
        """Resolve settings, filters and both channel entities. None if the job can't run."""
        from app.repositories import state_repo

        settings = state_repo.get_settings_dict()
        self._load_copy_settings(settings)

        blocked_words: list[str] = []
        if job.use_blocked_words:
            blocked_words = filter_repo.get_word_strings()
            self._log.info("Job #%d: %d blocked words loaded", job.id, len(blocked_words))

        src_rec = source_repo.get_source_by_id(job.source_id)
        dst_recs = [source_repo.get_destination_by_id(d) for d in job.destination_id_list()]
        if not src_rec or any(r is None for r in dst_recs):
            job_repo.update_status(job.id, "failed", error="מקור או יעד לא נמצאו")
            return None

        try:
            from app.worker.telegram_utils import get_entity_safe
            src_entity = await get_entity_safe(
                self._client, str(src_rec.resolved_id or src_rec.channel_ref)
            )
            # Every destination must resolve: any message may be routed to any of
            # them, so failing one means this account cannot run the job at all.
            dst_targets: list[tuple[int, object]] = []
            for dst_rec in dst_recs:
                dst_entity = await get_entity_safe(
                    self._client, str(dst_rec.resolved_id or dst_rec.channel_ref)
                )
                dst_targets.append((dst_rec.id, dst_entity))
        except (ChannelPrivateError, ValueError) as e:
            # This account cannot see the channel. Let the worker offer the job to
            # another userbot instead of failing it outright.
            self._log.warning(
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

        for dst_rec, (_, dst_entity) in zip(dst_recs, dst_targets):
            if not dst_rec.resolved_id:
                try:
                    source_repo.update_destination_resolved(
                        dst_rec.id,
                        getattr(dst_entity, "title", dst_rec.channel_ref),
                        dst_entity.id,
                    )
                except Exception:  # nosec B110 — best-effort cache update, non-fatal
                    pass

        return _JobContext(
            src_rec=src_rec,
            src_entity=src_entity,
            dst_targets=dst_targets,
            blocked_words=blocked_words,
            skip_duplicates=settings.get("skip_duplicates", "0") == "1",
            # Known-protected sources start the run already knowing it, so a job
            # that restarts does not re-learn it at the cost of another refused
            # forward — the very call that counts against the flood quota.
            protection=_SourceProtection(
                is_protected=bool(getattr(src_rec, "forwards_restricted", None))
            ),
        )

    async def _finalize(self, job: Job, ctx: _JobContext) -> bool:
        """
        Close a job out: terminal status, retry pass, Telegraph report.

        Returns True only for the account that won the terminal write. That is
        also the answer to "should I announce this job", which is why it is passed
        all the way back to the runner: on a sharded job several accounts reach
        this method, and every one of them announcing sent the user a pile of
        identical completion messages.
        """
        # The source can run out at the same moment the user cancels. Never write a
        # terminal state over 'cancelled' — that silently undid the cancel and left
        # the job looking like it had completed normally.
        if job_repo.should_stop(job.id):
            self._log.info("Job #%d: stopped by user at end of run", job.id)
            return False

        # Whoever closes the last chunk gets here, and on a sharded job that can be
        # two accounts at once. The terminal write is the race: only the account
        # that wins it goes on to retry, log and report, so each happens once.
        if job.continuous:
            # A continuous job doesn't finish — it graduates from copying history
            # to listening for new messages. The worker picks it up as a listener
            # on its next reconcile.
            if not job_repo.mark_backfill_done(job.id):
                return False
        else:
            if not job_repo.mark_completed(job.id):
                return False

        # One more attempt at whatever failed, while the channels are still
        # resolved. Runs before the report so it only lists what stayed broken.
        # Best-effort by design: the job's terminal state is already written, and
        # letting a FloodWait out of here would send the caller's error handling
        # after a job that is finished — rewriting 'completed' into 'waiting_retry'.
        try:
            await self._retry_failed(job, ctx)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self._log.warning("Job #%d: retry pass stopped early (%s)", job.id, e)

        # Persist the pair's sync watermark now that the history is fully covered.
        # No-op unless this is a full-history, single-destination job — see
        # channel_sync_repo.record_from_job. Best-effort: a bookkeeping write must
        # never turn a finished job into a failed one.
        try:
            from app.repositories import channel_sync_repo
            channel_sync_repo.record_from_job(job)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self._log.warning("Job #%d: could not record sync watermark (%s)", job.id, e)

        fresh = job_repo.get_by_id(job.id) or job
        if job.continuous:
            self._log.info(
                "Job #%d: history copy finished (copied=%d skipped=%d failed=%d) "
                "— switching to live listening",
                job.id, fresh.copied_count, fresh.skipped_count, fresh.failed_count,
            )
        else:
            self._log.info(
                "Job #%d completed: copied=%d skipped=%d failed=%d",
                job.id, fresh.copied_count, fresh.skipped_count, fresh.failed_count,
            )

        # Generate Telegraph report for notable (failed / unexpected-skipped) messages
        report_msgs = job_repo.get_report_messages(job.id)
        if not report_msgs:
            self._log.info("Job #%d: no notable messages — Telegraph report skipped", job.id)
            return True
        from app.services import telegraph_service
        url = await telegraph_service.create_report(
            job.id, report_msgs, ctx.src_rec.resolved_id, ctx.src_rec.channel_ref
        )
        if url:
            job_repo.save_report_url(job.id, url)
            self._log.info("Job #%d Telegraph report: %s", job.id, url)
        return True

    async def _retry_failed(self, job: Job, ctx: _JobContext) -> None:
        """
        Give every message that failed one more attempt, once the pass is over.

        Most failures are transient — a busy Telegram worker, an expired file
        reference, a retry budget spent on a bad minute. Until now they were only
        written to the report, so anyone who didn't read it lost them silently.

        Exactly one attempt per message: a message that fails twice has a real
        problem, and looping on it would hold the job open indefinitely.
        """
        failed_ids = job_repo.get_failed_source_ids(job.id)
        if not failed_ids:
            return

        recovered = 0
        reclassified = 0
        for i, msg_id in enumerate(failed_ids):
            if job_repo.should_stop(job.id):
                break
            # This pass sends for real and spends the same daily quota the main
            # pass does, but it runs after the job's terminal state is written, so
            # the "capped" hand-back the main loop uses no longer applies. Without
            # this check an account that finished a job right on its limit went on
            # to push hundreds of messages past it.
            if i % _RETRY_CAP_CHECK_EVERY == 0 and self._daily_cap_reached():
                self._log.warning(
                    "Job #%d: retry pass stopped at %d/%d — userbot #%s is out of "
                    "daily quota. The remaining failures stay in the report.",
                    job.id, i, len(failed_ids), self._userbot_id,
                )
                break
            try:
                msg = await self._flood_retry(
                    lambda: self._client.get_messages(ctx.src_entity, ids=msg_id),
                    f"Job #{job.id}: retry fetch of msg #{msg_id}",
                )
            except FloodWaitError:
                raise
            except Exception as e:  # pylint: disable=broad-exception-caught
                self._log.debug("Job #%d: retry could not fetch msg #%d: %s", job.id, msg_id, e)
                continue
            if msg is None or not getattr(msg, "id", None):
                continue  # deleted at the source since the failure

            dest_id, dst_entity = random.choice(ctx.dst_targets)  # nosec B311
            try:
                status, skip_reason = await self._flood_retry(
                    lambda: self._process_message(
                        job, msg, ctx.blocked_words, ctx.src_entity, dst_entity,
                        ctx.protection, skip_duplicates=ctx.skip_duplicates,
                    ),
                    f"Job #{job.id}: retry of msg #{msg_id}",
                )
            except FloodWaitError:
                raise
            except Exception as e:  # pylint: disable=broad-exception-caught
                # One bad message must not end the pass for all the others — this
                # is the last chance every remaining failure gets.
                self._log.debug("Job #%d: retry of msg #%d raised: %s", job.id, msg_id, e)
                continue

            if status == "failed":
                continue  # still broken; its row already says so

            # A message that now comes back 'skipped' (blocked word, duplicate, a
            # content type the job no longer takes) is not a failure any more.
            # Leaving the row at 'failed' kept it in the counters and in the
            # report as though the copy had gone wrong.
            job_repo.record_copied_message(
                job.id, msg.id, None, status, skip_reason, userbot_id=self._userbot_id
            )
            if status == "copied":
                self._record_transfer(job, msg, dest_id)
                # The message moved out of 'failed'. add_progress is a plain sum,
                # so the -1 is safe.
                job_repo.add_progress(job.id, copied=1, failed=-1)
                recovered += 1
                await self._rate_limiter.wait(dest_id=dest_id)
            else:
                job_repo.add_progress(job.id, skipped=1, failed=-1)
                reclassified += 1

        self._log.info(
            "Job #%d: retried %d failed message(s) — %d recovered, %d reclassified as skipped",
            job.id, len(failed_ids), recovered, reclassified,
        )

    # ── One copy pass ──────────────────────────────────────────────────────────

    async def _copy_stream(
        self, job: Job, ctx: _JobContext, chunk: Optional[JobChunk] = None
    ) -> str:
        """
        Copy one range of the job — the whole of it, or a single chunk.

        Returns:
          "completed" — the range was copied to the end
          "stopped"   — the user paused or cancelled the job
          "capped"    — this account ran out of daily quota part-way
        """
        group_media: bool = job.group_media
        skip_duplicates: bool = ctx.skip_duplicates
        blocked_words: list[str] = ctx.blocked_words
        src_entity = ctx.src_entity
        dst_targets = ctx.dst_targets
        dest_ids = [d for d, _ in dst_targets]

        # Only this range's history is relevant; loading a big job's whole record
        # for every chunk would be waste.
        if chunk is None:
            already_done: set[int] = job_repo.get_copied_source_ids(job.id)
            self._log.info(
                "Job #%d: resuming — %d already done, checkpoint=#%s",
                job.id, len(already_done), job.last_processed_id,
            )
        else:
            already_done = job_repo.get_copied_source_ids(job.id, chunk.id_from, chunk.id_to)
            self._log.info(
                "Job #%d chunk #%d (ids %d–%d): %d already done, checkpoint=#%s",
                job.id, chunk.id, chunk.id_from, chunk.id_to,
                len(already_done), chunk.last_processed_id,
            )

        p = _Progress(job.id, chunk.id if chunk else None)
        _last_progress_log = 0
        _msgs_since_pause_check = 0  # check for pause every 25 messages
        _msgs_since_limit_check = 0  # check daily limit every 100 messages
        _last_retry_reset = 0

        def maybe_reset_retry() -> None:
            """
            Clear the job's retry counter after a long, trouble-free stretch.

            The counter is only meaningful as a measure of *consecutive* trouble.
            Nothing used to reset it mid-run, so FloodWaits hours apart added up
            until an unrelated one crossed max_retries and paused a job that was
            copying perfectly well.
            """
            nonlocal _last_retry_reset
            if p.copied - _last_retry_reset >= _RETRY_RESET_AFTER_COPIES:
                _last_retry_reset = p.copied
                job_repo.reset_retry(job.id)

        # Buffer for collecting media-group messages before forwarding them together
        pending_group: list[Message] = []
        current_group_id: Optional[int] = None

        # Buffer for grouping individually-sent photos/videos into albums (group_media feature)
        solo_media_buffer: list[Message] = []

        async def flush_solo_media() -> bool:
            """Flush solo media buffer. Returns True if the job must stop (paused/cancelled)."""
            nonlocal solo_media_buffer
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
            allowed_types: set[str] = set((job.content_types or DEFAULT_CONTENT_TYPES).split(","))
            to_send: list[Message] = []
            for m in buffer:
                if not job.copy_text and not _has_transferable_file(m):
                    job_repo.record_copied_message(job.id, m.id, None, "skipped", "text_stripped_empty", userbot_id=self._userbot_id)
                    already_done.add(m.id)
                    p.skipped += 1
                    continue
                if blocked_words and self._is_blocked(m, blocked_words):
                    job_repo.record_copied_message(job.id, m.id, None, "skipped", "blocked_word", userbot_id=self._userbot_id)
                    already_done.add(m.id)
                    p.skipped += 1
                    continue
                if allowed_types != ALL_CONTENT_TYPES:
                    msg_type = self._get_content_type(m)
                    if msg_type not in allowed_types:
                        job_repo.record_copied_message(job.id, m.id, None, "skipped", f"content_type:{msg_type}", userbot_id=self._userbot_id)
                        already_done.add(m.id)
                        p.skipped += 1
                        continue
                if skip_duplicates and dedup_repo.is_duplicate_any(m, dest_ids):
                    job_repo.record_copied_message(job.id, m.id, None, "skipped", "duplicate", userbot_id=self._userbot_id)
                    already_done.add(m.id)
                    p.skipped += 1
                    continue
                to_send.append(m)

            if not to_send:
                p.flush(checkpoint)
                return False

            # One random destination per synthetic album — the whole batch (and
            # any individual fallback send) must land in a single channel.
            dest_id, dst_entity = random.choice(dst_targets)  # nosec B311

            async def _send_single(m: Message) -> tuple[str, str | None]:
                """Forward one message; returns (status, reason). Updates ctx.protection."""
                if ctx.protection.is_protected:
                    return await self._copy_protected(job, m, dst_entity, ctx.protection)
                try:
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
                    await self._note_protected(job, ctx.protection)
                    return await self._copy_protected(job, m, dst_entity, ctx.protection)
                except FloodWaitError:
                    raise
                except Exception as e:
                    return "failed", str(e)[:200]

            if len(to_send) == 1:
                st, reason = await self._flood_retry(
                    lambda: _send_single(to_send[0]),
                    f"Job #{job.id}: solo media #{to_send[0].id}",
                )
                if st == "copied":
                    p.copied += 1
                    self._record_transfer(job, to_send[0], dest_id)
                else:
                    p.failed += 1
                job_repo.record_copied_message(job.id, to_send[0].id, None, st, reason, userbot_id=self._userbot_id)
                already_done.add(to_send[0].id)
            else:
                # Try fast album send via file refs; fall back to individual forwards (not download)
                album_ok = False
                try:
                    await self._flood_retry(
                        lambda: self._send_group_by_ref(
                            to_send, dst_entity, copy_text=job.copy_text
                        ),
                        f"Job #{job.id}: solo-media album of {len(to_send)}",
                    )
                    album_ok = True
                    self._log.info(
                        "Job #%d: grouped %d solo media into album (ids=%s)",
                        job.id, len(to_send), [m.id for m in to_send],
                    )
                except FloodWaitError:
                    raise
                except Exception as ref_err:
                    self._log.warning(
                        "Job #%d: album ref-send failed (%s) — falling back to %d individual sends",
                        job.id, ref_err, len(to_send),
                    )

                if album_ok:
                    for m in to_send:
                        job_repo.record_copied_message(job.id, m.id, None, "copied", None, userbot_id=self._userbot_id)
                        self._record_transfer(job, m, dest_id)
                        already_done.add(m.id)
                        p.copied += 1
                else:
                    # Send the first message individually, then put the rest back
                    # into the buffer so they can form a new album.
                    #
                    # There is nothing to inspect that would identify the real
                    # culprit: SendMultiMediaRequest reports the batch, not the
                    # item. The one cause we *can* recognise — a caption over the
                    # limit — is kept out of the buffer entirely further down, so
                    # it never reaches this batch in the first place.
                    culprit = to_send[0]
                    st, reason = await self._flood_retry(
                        lambda: _send_single(culprit),
                        f"Job #{job.id}: solo media #{culprit.id}",
                    )
                    if st == "copied":
                        p.copied += 1
                        self._record_transfer(job, culprit, dest_id)
                    else:
                        p.failed += 1
                        self._log.warning(
                            "Job #%d: failed to send msg #%d individually: %s",
                            job.id, culprit.id, reason,
                        )
                    job_repo.record_copied_message(job.id, culprit.id, None, st, reason, userbot_id=self._userbot_id)
                    already_done.add(culprit.id)

                    # Re-queue the remaining messages for the next album attempt
                    remaining = [m for m in to_send if m.id != culprit.id]
                    if remaining:
                        self._log.info(
                            "Job #%d: re-queuing %d messages back to solo buffer after album failure",
                            job.id, len(remaining),
                        )
                        solo_media_buffer = remaining + solo_media_buffer
                        # The re-queued messages are neither sent nor recorded. A
                        # checkpoint past them would make the next run start after
                        # them (_fetch_messages resumes at last_processed_id), so
                        # they would be dropped for good if the job stops here.
                        # Messages are buffered in ascending id order, so the
                        # lowest re-queued id is the first one not yet accounted
                        # for. The message just sent is below it and is recorded in
                        # copied_messages, so it is not re-sent on resume.
                        checkpoint = remaining[0].id - 1

            p.flush(checkpoint)
            if job_repo.should_stop(job.id):
                self._log.info("Job #%d: stop requested (paused/cancelled) after media flush at #%d", job.id, checkpoint)
                return True
            maybe_reset_retry()
            await self._rate_limiter.wait(album=True, count=len(to_send), dest_id=dest_id)
            return False

        async def flush_group() -> bool:
            """Flush pending album group. Returns True if the job must stop (paused/cancelled)."""
            nonlocal pending_group, current_group_id
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

            # An existing album is forwarded whole to one random destination.
            dest_id, dst_entity = random.choice(dst_targets)  # nosec B311
            statuses = await self._flood_retry(
                lambda: self._process_group(
                    job, pending, blocked_words, src_entity, dst_entity,
                    ctx.protection, skip_duplicates=skip_duplicates,
                ),
                f"Job #{job.id}: album of {len(pending)}",
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
                    p.copied += 1
                    self._record_transfer(job, msg, dest_id)
                elif status == "skipped":
                    p.skipped += 1
                else:
                    p.failed += 1

            p.flush(last_id)
            if job_repo.should_stop(job.id):
                self._log.info("Job #%d: stop requested (paused/cancelled) after album flush at #%d", job.id, last_id)
                return True
            # A fully skipped album sent nothing — pay no delay for it.
            copied_now = sum(1 for status, _ in statuses if status == "copied")
            maybe_reset_retry()
            if copied_now:
                await self._rate_limiter.wait(album=True, count=copied_now, dest_id=dest_id)
            return False

        try:
            async for msg in self._fetch_messages(job, src_entity, chunk):
                if msg is None or not hasattr(msg, "id"):
                    continue

                if msg.grouped_id:
                    # Existing album: flush solo buffer first, then accumulate
                    if group_media:
                        if await flush_solo_media():
                            return "stopped"
                    if msg.grouped_id == current_group_id:
                        pending_group.append(msg)
                    else:
                        if await flush_group():
                            return "stopped"
                        current_group_id = msg.grouped_id
                        pending_group = [msg]
                else:
                    # Individual message: flush any pending album group first
                    if await flush_group():
                        return "stopped"

                    # A caption over the limit would be rejected by
                    # SendMultiMediaRequest and take the whole album with it. The
                    # individual path below forwards the message with its text
                    # intact, so such a message never joins the buffer.
                    if group_media and self._is_groupable(msg) and not self._caption_too_long(msg):
                        # Add to solo buffer (skip if already done)
                        if msg.id not in already_done:
                            solo_media_buffer.append(msg)
                        if len(solo_media_buffer) >= 10:
                            if await flush_solo_media():
                                return "stopped"
                    else:
                        # Non-groupable: flush solo buffer, then process normally
                        if group_media:
                            if await flush_solo_media():
                                return "stopped"

                        if msg.id in already_done:
                            continue

                        dest_id, dst_entity = random.choice(dst_targets)  # nosec B311
                        status, skip_reason = await self._flood_retry(
                            lambda: self._process_message(
                                job, msg, blocked_words, src_entity, dst_entity,
                                ctx.protection, skip_duplicates=skip_duplicates,
                            ),
                            f"Job #{job.id}: msg #{msg.id}",
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
                            p.copied += 1
                            self._record_transfer(job, msg, dest_id)
                        elif status == "skipped":
                            p.skipped += 1
                        else:
                            p.failed += 1

                        p.flush(msg.id)
                        if p.copied - _last_progress_log >= 50:
                            _last_progress_log = p.copied
                            self._log.info(
                                "Job #%d progress: copied=%d skipped=%d failed=%d last_id=#%d",
                                job.id, p.copied, p.skipped, p.failed, msg.id,
                            )
                        # Checked every message, not every 25: it is one indexed
                        # primary-key lookup, which is nothing next to the 2–5s
                        # rate-limiter sleep between sends — and it makes cancel
                        # take effect on the next message instead of 25 later.
                        if job_repo.should_stop(job.id):
                            self._log.info("Job #%d: stop requested (paused/cancelled) — stopping at msg #%d", job.id, msg.id)
                            return "stopped"

                        _msgs_since_pause_check += 1
                        if _msgs_since_pause_check >= 25:
                            _msgs_since_pause_check = 0
                            if self._resolve_callback:
                                await self._resolve_callback()

                        _msgs_since_limit_check += 1
                        if _msgs_since_limit_check >= 100 and self._userbot_id is not None:
                            _msgs_since_limit_check = 0
                            from app.ui.texts import DAILY_LIMIT
                            # The cap belongs to this account, not to the job or the
                            # chunk: hand the work back so an account with budget
                            # resumes it from the checkpoint (copied_messages stops
                            # any re-copying). This runner stops claiming until
                            # midnight, so it cannot take it straight back. Only when
                            # every account is capped does the queue wait — see
                            # park_queue_if_all_capped.
                            count_today = job_repo.get_daily_count_for_userbot(self._userbot_id)
                            if count_today >= DAILY_LIMIT:
                                self._log.warning(
                                    "Job #%d: userbot #%d hit its daily limit mid-run (%d msgs) — "
                                    "releasing at msg #%d for another account",
                                    job.id, self._userbot_id, count_today, msg.id,
                                )
                                return "capped"

                        maybe_reset_retry()

                        # Skipped messages sent nothing to Telegram — pay no delay
                        # and don't advance the batch-pause counter for them.
                        if status == "copied":
                            await self._rate_limiter.wait(dest_id=dest_id)
                        elif status == "failed":
                            # A failed send still hit the network — brief fixed pause.
                            await asyncio.sleep(1.0)

            # Flush any remaining buffers at end of stream
            if await flush_group():
                return "stopped"
            if group_media:
                while solo_media_buffer:
                    if await flush_solo_media():
                        return "stopped"

        except FloodWaitError:
            self._log.warning("Job #%d: FloodWait encountered", job.id)
            raise

        except (ChatWriteForbiddenError, ChannelPrivateError) as e:
            # Access lost mid-run. Progress is checkpointed, so another userbot
            # can pick this up and resume from where this one stopped.
            self._log.warning(
                "Job #%d: userbot %s lost access mid-run (%s) — requesting reassignment",
                job.id, self._userbot_id, e,
            )
            raise NoAccessError(f"אין הרשאת גישה/כתיבה לערוץ: {e}") from e

        except Exception as e:
            self._log.exception("Job #%d: unexpected error: %s", job.id, e)
            raise

        return "completed"

    # ── Message fetching ───────────────────────────────────────────────────────

    async def _fetch_messages(
        self, job: Job, src_entity, chunk: Optional[JobChunk] = None
    ) -> AsyncIterator[Message]:
        """Yield messages in ascending ID order (oldest first) for safe resume."""
        client = self._client

        if chunk is not None:
            async for msg in self._fetch_chunk_messages(job, src_entity, chunk):
                yield msg
            return

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

    async def _fetch_chunk_messages(
        self, job: Job, src_entity, chunk: JobChunk
    ) -> AsyncIterator[Message]:
        """
        Yield one chunk's messages, ascending.

        The bounds are the chunk's, and so is the checkpoint: the job-wide one is
        meaningless while other accounts are copying other parts of the range.
        'id_range' needs no extra filtering because the chunk plan is already cut
        from that range; 'date_range' still filters here, since its chunks are cut
        from the channel's whole ID span.
        """
        client = self._client
        min_id = max(chunk.id_from - 1, chunk.last_processed_id or 0)
        max_id = chunk.id_to + 1

        date_from = _parse_date(job.date_from) if job.mode == "date_range" else None
        date_to = _parse_date(job.date_to) if job.mode == "date_range" else None

        async for msg in client.iter_messages(
            src_entity, reverse=True, min_id=min_id, max_id=max_id
        ):
            if date_from or date_to:
                if not msg.date:
                    continue
                msg_date = _as_aware_utc(msg.date)
                if date_from and msg_date < date_from:
                    continue
                if date_to and msg_date > date_to:
                    break
            yield msg

    # ── Message processing ─────────────────────────────────────────────────────

    async def _process_group(
        self,
        job: Job,
        group: list[Message],
        blocked_words: list[str],
        src_entity,
        dst_entity,
        protection: _SourceProtection,
        skip_duplicates: bool = False,
    ) -> list[tuple[str, Optional[str]]]:
        """
        Forward a media-group (album) as a single batch. Returns one status per message.

        `protection` is updated in place if the channel turns out to be protected.
        """
        # Global block word checks
        if blocked_words and any(self._is_blocked(m, blocked_words) for m in group):
            self._log.debug("Job #%d: group %d blocked by filter", job.id, group[0].grouped_id)
            return [("skipped", "blocked_word")] * len(group)

        allowed_types: set[str] = set((job.content_types or DEFAULT_CONTENT_TYPES).split(","))

        final_statuses: list[tuple[str, Optional[str]]] = []
        send_group: list[Message] = []

        # Filter items individually
        for m in group:
            if not job.copy_text and not _has_transferable_file(m):
                final_statuses.append(("skipped", "text_stripped_empty"))
                continue
            
            if allowed_types != ALL_CONTENT_TYPES:
                msg_type = self._get_content_type(m)
                if msg_type not in allowed_types:
                    final_statuses.append(("skipped", f"content_type:{msg_type}"))
                    continue

            if skip_duplicates and dedup_repo.is_duplicate_any(m, job.destination_id_list()):
                final_statuses.append(("skipped", "duplicate"))
                continue

            final_statuses.append(None) # placeholder
            send_group.append(m)

        if not send_group:
            self._log.debug("Job #%d: album group=%s all items skipped", job.id, group[0].grouped_id)
            return [st for st in final_statuses if st is not None]

        def fill_statuses(st_tuple):
            return [st_tuple if st is None else st for st in final_statuses]

        def fill_each(results: list[tuple[str, Optional[str]]]):
            """Slot per-item results into the placeholders, in order."""
            it = iter(results)
            return [next(it) if st is None else st for st in final_statuses]

        if len(send_group) == 1:
            st, reason = await self._process_message(
                job, send_group[0], [], src_entity, dst_entity, protection
            )
            return fill_statuses((st, reason))

        if protection.is_protected:
            # Known protected: no point spending a forward that can only be
            # refused. _send_group_as_copy carries the album over by file
            # reference and only downloads if that is refused and allowed.
            try:
                await self._send_group_as_copy(
                    send_group, dst_entity, copy_text=job.copy_text,
                    allow_download=self._allow_download_upload,
                )
                return fill_statuses(("copied", None))
            except FloodWaitError:
                raise
            except Exception as e:
                return fill_each(await self._copy_group_individually(
                    job, send_group, dst_entity, protection, e
                ))

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
            self._log.info(
                "Job #%d: forwarded album of %d items (ids=%s)",
                job.id, len(ids), ids,
            )
            return fill_statuses(("copied", None))

        except ChatForwardsRestrictedError:
            await self._note_protected(job, protection)
            try:
                await self._send_group_as_copy(
                    send_group, dst_entity, copy_text=job.copy_text,
                    allow_download=self._allow_download_upload,
                )
                return fill_statuses(("copied", None))
            except FloodWaitError:
                raise
            except Exception as e:
                return fill_each(await self._copy_group_individually(
                    job, send_group, dst_entity, protection, e
                ))

        except FloodWaitError:
            raise

        except Exception as e:
            self._log.warning(
                "Job #%d: failed to forward album (ids=%s): %s",
                job.id, ids, e,
            )
            return fill_statuses(("failed", str(e)[:200]))

    async def _process_message(
        self,
        job: Job,
        msg: Message,
        blocked_words: list[str],
        src_entity,
        dst_entity,
        protection: _SourceProtection,
        skip_duplicates: bool = False,
    ) -> tuple[str, Optional[str]]:
        """
        Copy one message. Returns (status, skip_reason).

        `protection` is updated in place the moment a forward is refused, so a
        re-attempt of this same message never repeats the refused call.
        """

        # Filter check
        if blocked_words and self._is_blocked(msg, blocked_words):
            self._log.debug("Job #%d: msg #%d blocked by filter", job.id, msg.id)
            return "skipped", "blocked_word"

        # Already sent this exact content to any of the job's destinations
        if skip_duplicates and dedup_repo.is_duplicate_any(msg, job.destination_id_list()):
            self._log.debug("Job #%d: msg #%d skipped as duplicate", job.id, msg.id)
            return "skipped", "duplicate"

        # Content type filter
        allowed_types: set[str] = set((job.content_types or DEFAULT_CONTENT_TYPES).split(","))
        if allowed_types != ALL_CONTENT_TYPES:
            msg_type = self._get_content_type(msg)
            if msg_type not in allowed_types:
                self._log.debug("Job #%d: msg #%d skipped (type=%s not in %s)", job.id, msg.id, msg_type, allowed_types)
                return "skipped", f"content_type:{msg_type}"

        # Supported type check
        if not self._is_supported_type(msg):
            self._log.debug("Job #%d: msg #%d unsupported type", job.id, msg.id)
            return "skipped", "unsupported_type"

        # Skip empty service messages
        if not msg.text and not msg.media:
            return "skipped", "empty_message"

        # `media` is not the same as "has a file to send": a link preview, a
        # contact or a venue all set .media and carry nothing sendable. Testing
        # the file, not the media, is what keeps such a message from being
        # recorded as copied when every send path quietly did nothing.
        if not job.copy_text and not _has_transferable_file(msg):
            return "skipped", "text_stripped_empty"
        if job.copy_text and not msg.text and not _has_transferable_file(msg):
            return "skipped", "empty_message"

        if protection.is_protected:
            # Known protected: sending the forward anyway would only spend an API
            # call on a refusal, and that call counts against the flood quota.
            return await self._copy_protected(job, msg, dst_entity, protection)

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
            return "copied", None

        except ChatForwardsRestrictedError:
            await self._note_protected(job, protection)
            return await self._copy_protected(job, msg, dst_entity, protection)

        except FloodWaitError:
            raise

        except Exception as e:
            self._log.warning("Job #%d: failed to copy msg #%d: %s", job.id, msg.id, e)
            return "failed", str(e)[:200]

    async def _note_protected(self, job: Job, protection: _SourceProtection) -> None:
        """
        Telegram just refused to forward out of this source. Remember it.

        Written to the source row as well as to this run's state, so the next job
        on the same channel starts out knowing it and never spends the refused
        forward again. The first time a channel turns out to be protected the
        admin is told, since it changes what the copy can and cannot do.
        """
        if protection.is_protected:
            return
        protection.is_protected = True
        self._log.info(
            "Job #%d: source channel blocks forwarding — copying by file reference from here on",
            job.id,
        )
        try:
            from app.worker import worker_main
            await worker_main.note_forwards_restricted(job.source_id)
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Bookkeeping and notification must never take a copy down with them.
            self._log.debug("Job #%d: could not record forwards_restricted: %s", job.id, e)

    async def _copy_group_individually(
        self,
        job: Job,
        group: list[Message],
        dst_entity,
        protection: _SourceProtection,
        album_error: Exception,
    ) -> list[tuple[str, Optional[str]]]:
        """
        Send a protected album one message at a time. One status per message.

        The album API is far narrower than a plain send: `SendMultiMediaRequest`
        takes photos and ordinary videos only, so a group holding a GIF, a plain
        document or a round note cannot go as an album at all. That used to mean
        the whole group failed once the download fallback was switched off — even
        though `send_file(dst, msg.media)` carries every one of those types by
        reference perfectly well. The items simply arrive unglued rather than as
        an album, which is the right trade against losing them.

        Per-message statuses, not one for the batch: a partial send here is real,
        and recording the whole group as copied would lose whatever did not go.
        """
        self._log.warning(
            "Job #%d: album of %d could not be sent as a group (%s) — sending "
            "the items individually by reference",
            job.id, len(group), album_error,
        )
        results: list[tuple[str, Optional[str]]] = []
        for m in group:
            results.append(await self._flood_retry(
                lambda msg=m: self._copy_protected(job, msg, dst_entity, protection),
                f"Job #{job.id}: album item #{m.id}",
            ))
        return results

    async def _report_copy_blocked(
        self, job: Job, protection: _SourceProtection, error: Exception
    ) -> None:
        """Alert once per run that this job cannot copy at all. Never raises."""
        if protection.blocked_reported:
            return
        protection.blocked_reported = True
        self._log.error(
            "Job #%d: the fast path was refused and download+upload is off — "
            "messages will be recorded as failed until it is turned on",
            job.id,
        )
        try:
            from app.worker import worker_main
            await worker_main.send_copy_blocked_notification(
                job.id, job.source_id, str(error)[:200]
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            self._log.debug("Job #%d: could not send copy-blocked alert: %s", job.id, e)

    async def _copy_protected(
        self, job: Job, msg: Message, dst_entity, protection: _SourceProtection
    ) -> tuple[str, Optional[str]]:
        """
        Copy one message out of a channel that blocks forwarding.

        By file reference first, always: `send_file(dst, msg.media)` hands
        Telegram the copy it already has, so nothing is downloaded and nothing is
        uploaded no matter how large the file is. Download+re-upload stays as an
        emergency route only, and only when the operator has switched it on —
        otherwise a job that hits it would crawl for hours unnoticed.
        """
        # Neither route sends anything for a message with no file and no text —
        # a bare contact, venue or dice. Saying "copied" for one of those would
        # record a message, spend a dedup entry and pay a rate-limiter delay for
        # a send that never happened.
        text = msg.text if job.copy_text else ""
        if not _has_transferable_file(msg) and not text:
            return "skipped", "empty_message"

        # Once a by-ref send has worked on this pair of channels the method is
        # proven; a later failure is that one message's problem, not the route's.
        # With the download route off there is nothing to fall back to, so keep
        # trying by ref regardless of what the last message did.
        if protection.ref_send_works is not False or not self._allow_download_upload:
            try:
                await self._send_by_ref(msg, dst_entity, copy_text=job.copy_text)
                protection.ref_send_works = True
                return "copied", None
            except FloodWaitError:
                raise
            except Exception as e:  # pylint: disable=broad-exception-caught
                if protection.ref_send_works is None:
                    protection.ref_send_works = False
                self._log.warning(
                    "Job #%d: by-reference send failed for msg #%d: %s", job.id, msg.id, e
                )
                if not self._allow_download_upload:
                    # Fail loudly rather than quietly falling into hours of
                    # downloading. "Loudly" has to mean an alert of its own: a
                    # source already on record as protected produces no
                    # transition, so the discovery notification never fires and
                    # the whole job would fail message by message in silence.
                    await self._report_copy_blocked(job, protection, e)
                    return "failed", "forwards_restricted"

        size_mb = _media_size_mb(msg)
        if size_mb is not None and size_mb > self._max_download_mb:
            self._log.warning(
                "Job #%d: msg #%d is %.0fMB, over the %dMB download ceiling — skipped",
                job.id, msg.id, size_mb, self._max_download_mb,
            )
            return "skipped", "file_too_large"

        try:
            await self._send_as_copy(msg, dst_entity, copy_text=job.copy_text)
            return "copied", None
        except FloodWaitError:
            raise
        except Exception as e:  # pylint: disable=broad-exception-caught
            self._log.warning("Job #%d: failed to copy msg #%d: %s", job.id, msg.id, e)
            return "failed", str(e)[:200]

    def _record_transfer(self, job: Job, msg: Message, destination_id: int) -> None:
        """Add a successfully transferred message to the global dedup registry."""
        dedup_repo.record_message(
            msg,
            destination_id=destination_id,
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
        self._load_copy_settings(settings)
        skip_duplicates = settings.get("skip_duplicates", "0") == "1"

        if msg is None or not hasattr(msg, "id"):
            return "skipped"

        # Never process the same source message twice.
        if job_repo.is_message_processed(job.id, msg.id):
            return "skipped"

        blocked_words: list[str] = filter_repo.get_word_strings() if job.use_blocked_words else []

        src_rec = source_repo.get_source_by_id(job.source_id)
        dst_recs = [source_repo.get_destination_by_id(d) for d in job.destination_id_list()]
        if not src_rec or any(r is None for r in dst_recs):
            return "failed"

        from app.worker.telegram_utils import get_entity_safe
        try:
            src_entity = await get_entity_safe(
                self._client, str(src_rec.resolved_id or src_rec.channel_ref)
            )
            dst_targets: list[tuple[int, object]] = []
            for dst_rec in dst_recs:
                dst_targets.append((dst_rec.id, await get_entity_safe(
                    self._client, str(dst_rec.resolved_id or dst_rec.channel_ref)
                )))
        except (ChannelPrivateError, ValueError) as e:
            raise NoAccessError(f"אין גישה לערוץ: {e}") from e

        dest_id, dst_entity = random.choice(dst_targets)  # nosec B311

        # One message, one call: the protection state lives only for this send,
        # but it starts from what the source row already knows so a live copy out
        # of a protected channel skips the refused forward too.
        protection = _SourceProtection(
            is_protected=bool(getattr(src_rec, "forwards_restricted", None))
        )
        status, skip_reason = await self._flood_retry(
            lambda: self._process_message(
                job, msg, blocked_words, src_entity, dst_entity, protection,
                skip_duplicates=skip_duplicates,
            ),
            f"Job #{job.id} (continuous): live msg #{msg.id}",
        )

        job_repo.record_copied_message(job.id, msg.id, None, status, skip_reason, userbot_id=self._userbot_id)

        job_repo.add_progress(
            job.id,
            copied=1 if status == "copied" else 0,
            skipped=1 if status == "skipped" else 0,
            failed=1 if status == "failed" else 0,
            last_processed_id=msg.id,
        )

        if status == "copied":
            self._record_transfer(job, msg, dest_id)
            await self._rate_limiter.wait(dest_id=dest_id)

        self._log.info(
            "Job #%d (continuous): live msg #%d → %s%s",
            job.id, msg.id, status, f" ({skip_reason})" if skip_reason else "",
        )
        return status

    # ── Hyper backup ───────────────────────────────────────────────────────────

    async def handle_hyper_message(
        self,
        dst_rec,
        msg: Message,
        rules: dict,
        all_dest_ids: list[int],
        is_capped: Optional[Callable[[], bool]] = None,
    ) -> str:
        """
        Back up one outgoing message to one of the account's hyper backup channels.

        `dst_rec` is the single channel this message is sent to — the caller picks
        it at random from the account's backup channels (fan-out, like a job's
        multi-destination). `all_dest_ids` is the full set of those channels, used
        only for the duplicate check below.

        Media only (text returns 'skipped'), gated by the per-account smart
        filter, and always de-duplicated across *all* backup channels — hyper's
        whole point is "don't store the same file twice", so a file already sent
        to any of the account's backup channels is skipped here, regardless of the
        global skip_duplicates setting. The loop-guard that stops us backing up our
        own backup lives in the caller (it compares the event's chat to the set of
        backup channels before we ever get here).

        `is_capped` is checked only *after* the filter and dedup pass, so an item
        that would be sent but for the daily cap returns 'queued' (the caller
        parks it for later) — we never queue junk or duplicates.

        Returns: copied | skipped | queued | failed.
        """
        from app.services import hyper_filter
        from app.worker.telegram_utils import get_entity_safe

        if msg is None or not hasattr(msg, "id"):
            return "skipped"

        media_type = hyper_filter.hyper_media_type(msg)
        if media_type is None:
            return "skipped"  # text / service / unclassifiable — not backed up

        size, duration = hyper_filter.extract_size_duration(msg)
        passes, reason = hyper_filter.evaluate(media_type, size, duration, rules)
        if not passes:
            self._log.debug("Hyper: msg #%s skipped by filter (%s/%s)", msg.id, media_type, reason)
            return "skipped"

        # Content dedup, forced on and cross-account: the registry is keyed by
        # (destination, content), so whichever account already sent this file to
        # any of the backup channels makes every other account — and every other
        # backup channel in the fan-out — skip it.
        if dedup_repo.is_duplicate_any(msg, all_dest_ids):
            self._log.debug("Hyper: msg #%s already in backup — skipped", msg.id)
            return "skipped"

        # Out of daily quota: don't send now, let the caller queue it for later.
        # Checked here (not before the filter) so only real, non-duplicate work
        # is ever parked.
        if is_capped is not None and is_capped():
            return "queued"

        # Read only once there is something to send: hyper runs off a live event
        # handler, and this backup may be the only work this account ever does.
        # Cached, because this runs per message rather than per job.
        self._load_copy_settings_cached()

        try:
            dst_entity = await get_entity_safe(
                self._client, str(dst_rec.resolved_id or dst_rec.channel_ref)
            )
        except Exception as e:  # noqa: BLE001 — any resolution failure means we can't back up now
            self._log.warning("Hyper: cannot resolve backup channel (%s)", e)
            return "failed"

        try:
            # drop_author keeps the backup clean (no "forwarded from"); the source
            # peer is taken from the message itself.
            await self._flood_retry(
                lambda: self._client.forward_messages(dst_entity, msg, drop_author=True),
                f"Hyper: msg #{msg.id}",
            )
        except ChatForwardsRestrictedError:
            # Same rule as a job: hand Telegram the file reference it already has
            # rather than moving the bytes twice.
            try:
                await self._flood_retry(
                    lambda: self._send_by_ref(msg, dst_entity, copy_text=True),
                    f"Hyper: msg #{msg.id} (by ref)",
                )
            except FloodWaitError:
                raise
            except Exception as ref_err:  # noqa: BLE001
                self._log.warning("Hyper: by-reference send failed for #%s: %s", msg.id, ref_err)
                if not self._allow_download_upload:
                    return "failed"
                try:
                    await self._flood_retry(
                        lambda: self._send_as_copy(msg, dst_entity, copy_text=True),
                        f"Hyper: msg #{msg.id} (copy)",
                    )
                except FloodWaitError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self._log.warning("Hyper: download+upload fallback failed for #%s: %s", msg.id, e)
                    return "failed"
        except FloodWaitError:
            raise
        except Exception as e:  # noqa: BLE001
            self._log.warning("Hyper: forward failed for #%s: %s", msg.id, e)
            return "failed"

        self._record_hyper_transfer(dst_rec.id, msg)
        await self._rate_limiter.wait(dest_id=dst_rec.id)
        self._log.info("Hyper: backed up msg #%s (%s) → dest #%d", msg.id, media_type, dst_rec.id)
        return "copied"

    def _record_hyper_transfer(self, destination_id: int, msg: Message) -> None:
        """Register a hyper transfer: dedup registry + the per-account daily-cap counter."""
        from app.repositories import hyper_repo
        dedup_repo.record_message(msg, destination_id=destination_id, source_id=None, job_id=None)
        if self._userbot_id is not None:
            hyper_repo.record_send(self._userbot_id)

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

    async def _send_by_ref(self, msg: Message, dst_entity, copy_text: bool = True) -> None:
        """
        Send a single message using the file reference Telegram already holds.

        This is the fast path out of a channel that blocks forwarding: handing
        `msg.media` to send_file makes Telethon build an InputMediaDocument /
        InputMediaPhoto from the existing file_reference, so not one byte of the
        file travels in either direction — a 2GB video costs the same as a photo.
        Nothing is replayed by hand: the document carries its own filename, audio
        title and video attributes with it.

        The one thing a forward does that this cannot is carry an unlimited
        caption; a fresh send is subject to Telegram's 1024-character limit, so a
        longer text is truncated.
        """
        text = msg.text if copy_text else ""

        if not _has_transferable_file(msg):
            if text:
                await self._client.send_message(dst_entity, text)
            return

        await self._client.send_file(
            dst_entity,
            msg.media,
            caption=_truncate_caption(text) or None,
        )

    async def _send_as_copy(self, msg: Message, dst_entity, copy_text: bool = True) -> None:
        """Download and re-upload a single message — the emergency route only.

        Reached when a by-reference send was refused *and* the operator has turned
        `allow_download_upload` on. Raises RuntimeError if the media cannot be
        downloaded (caller records the message as failed)."""
        text = msg.text if copy_text else ""

        if not _has_transferable_file(msg):
            if text:
                await self._client.send_message(dst_entity, text)
            return

        # To disk, not to memory: `file=bytes` held the whole file in the
        # process's RAM, so one 2GB video both spiked memory and blocked the
        # shared event loop while it was assembled.
        path = await self._download_to_temp(msg)
        if path is None:
            # Media could not be downloaded (e.g. forwarded from protected channel)
            raise RuntimeError("download_failed: media returned None (protected or unavailable)")

        # A re-upload from disk keeps the filename, but an audio track's title and
        # a document's other attributes still have to be replayed, and
        # force_document keeps a document a document instead of letting Telethon
        # sniff an image file back into a photo.
        # Documents only: photos have no attributes to replay, and Telegram
        # rejects a sticker's attributes on a fresh upload.
        attributes = None
        force_document = False
        if self._get_content_type(msg) == "file":
            doc = getattr(msg.media, "document", None)
            if doc:
                attributes = list(doc.attributes)
                force_document = True

        try:
            # A protected source has to be re-uploaded, and a fresh upload is
            # subject to the caption limit — unlike a forward, which carries the
            # original text over however long it is.
            #
            # Retried here rather than only by the caller's _flood_retry: that one
            # wraps the whole message, so a FloodWait on the upload threw away a
            # download that had just finished and did the whole transfer again.
            await self._flood_retry(
                lambda: self._client.send_file(
                    dst_entity,
                    path,
                    caption=_truncate_caption(text) or None,
                    attributes=attributes,
                    force_document=force_document,
                ),
                f"upload of msg #{msg.id}",
            )
        finally:
            _discard_temp(path)

    async def _download_to_temp(self, msg: Message) -> Optional[str]:
        """
        Download one message's media into a staging directory of its own.

        A private directory per download, not a shared one: Telethon names the
        file after the source document, and its "does this name exist yet" check
        is a plain stat. Two accounts on this one event loop downloading files
        with the same name both saw "free", picked the same path, and one
        overwrote the other's media — then deleted it out from under the upload.
        """
        os.makedirs(_TEMP_MEDIA_DIR, exist_ok=True)
        staging = tempfile.mkdtemp(dir=_TEMP_MEDIA_DIR)
        try:
            path = await self._client.download_media(msg, file=staging)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if path is None:
            shutil.rmtree(staging, ignore_errors=True)
        return path

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
            # Solo-media grouping keeps over-long captions out of albums entirely,
            # but a real grouped_id album arrives as-is — truncating is the only
            # way to send it at all.
            #
            # SendMultiMediaRequest is the raw API: it takes the caption verbatim
            # and parses nothing, so the markup in .text would reach the channel
            # as literal characters. .message is the text as Telegram stores it.
            caption = _truncate_caption(_raw_text(m))
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

    async def _send_group_as_copy(
        self,
        group: list[Message],
        dst_entity,
        copy_text: bool = True,
        allow_download: bool = True,
    ) -> None:
        """
        Send a media group by file reference (fast), falling back to
        download+reupload when the album API cannot carry the items by reference.

        `allow_download` is what a protected source passes: there the fallback is
        the operator's `allow_download_upload` switch, and with it off the group
        fails loudly instead of quietly costing hours. An unprotected source
        leaves it on — the fallback there is about item types the album API
        rejects (plain docs, GIFs, round notes), not about protection.
        """
        try:
            await self._send_group_by_ref(group, dst_entity, copy_text=copy_text)
            return
        except FloodWaitError:
            raise
        except Exception as e:
            if not allow_download:
                raise RuntimeError(f"album_ref_send_failed: {e}") from e
            self._log.warning("Job: send_group_by_ref failed (%s) — falling back to download+upload", e)

        # Fallback: download to disk and re-upload. Files, not bytes — an album of
        # ten videos held in memory at once is what the loop stalls were made of.
        paths: list[str] = []
        captions: list[str] = []
        failed_downloads: list[Message] = []
        try:
            for m in group:
                if m.media and not isinstance(m.media, MessageMediaUnsupported):
                    size_mb = _media_size_mb(m)
                    if size_mb is not None and size_mb > self._max_download_mb:
                        raise RuntimeError(
                            f"file_too_large: msg #{m.id} is {size_mb:.0f}MB, "
                            f"over the {self._max_download_mb}MB ceiling"
                        )
                    path = await self._download_to_temp(m)
                    if path:
                        paths.append(path)
                        captions.append(_truncate_caption(m.text) if copy_text else "")
                    else:
                        failed_downloads.append(m)
                # text-only messages in a group are included via caption, no separate download needed

            if failed_downloads:
                # Raise so callers can record these as failed instead of silently dropping
                ids = [m.id for m in failed_downloads]
                raise RuntimeError(
                    f"download_failed: {len(failed_downloads)} media item(s) returned None (ids={ids})"
                )

            if not paths:
                text = next((m.text for m in group if m.text), None) if copy_text else None
                if text:
                    await self._client.send_message(dst_entity, text)
                return

            await self._client.send_file(dst_entity, paths, caption=captions)
        finally:
            for path in paths:
                _discard_temp(path)

    def _is_blocked(self, msg: Message, blocked_words: list[str]) -> bool:
        text = (msg.text or "").lower()
        return any(word in text for word in blocked_words)

    def _daily_cap_reached(self) -> bool:
        """
        True once this account has spent its daily quota.

        Same rule as the mid-run check in _copy_stream, pulled out so the retry
        pass — which runs outside that loop — is held to it too.
        """
        if self._userbot_id is None:
            return False
        from app.ui.texts import DAILY_LIMIT
        return job_repo.get_daily_count_for_userbot(self._userbot_id) >= DAILY_LIMIT

    @staticmethod
    def _caption_too_long(msg: Message) -> bool:
        """
        True if this message's text cannot survive as an album caption.

        Such a message is kept out of album grouping and forwarded on its own,
        where Telegram carries the original text over in full — truncating it
        would lose content that a plain forward keeps.
        """
        return _utf16_len(_raw_text(msg)) > _MAX_CAPTION_LEN

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
        """Classify message as 'text', 'image', 'video', 'file', or 'other'."""
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
                # Any other document: PDF, archive, music, voice note.
                # Only with a document to send — an expired one stays 'other'
                # so the filter drops it instead of failing the copy.
                return "file"
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

def _has_transferable_file(msg: Message) -> bool:
    """
    True if the message carries a photo or document that can be sent on.

    Not every `media` is a file: a link preview (MessageMediaWebPage), a contact
    or a venue has nothing to send, and handing one to send_file only produces a
    TypeError. Those messages are text, and are sent as text.
    """
    media = getattr(msg, "media", None)
    if not media or isinstance(media, MessageMediaUnsupported):
        return False
    return bool(getattr(media, "photo", None) or getattr(media, "document", None))


def _media_size_mb(msg: Message) -> Optional[float]:
    """
    A message's media size in MB, or None if Telegram didn't state one.

    Free: the size travels with the message we already have, so a file too big to
    be worth downloading is recognised before a single byte moves.
    """
    size = getattr(getattr(msg, "file", None), "size", None)
    if size is None:
        doc = getattr(getattr(msg, "media", None), "document", None)
        size = getattr(doc, "size", None)
    if not size:
        return None
    return size / (1024 * 1024)


def _int_setting(settings: dict[str, str], key: str, default: int) -> int:
    """Read one int out of an already-fetched settings dict, without another query."""
    try:
        return int(settings[key])
    except (KeyError, ValueError, TypeError):
        return default


def _discard_temp(path: Optional[str]) -> None:
    """
    Delete a staged download and the private directory it lives in.

    Never raises: by the time this runs the send has already happened, and a
    file left behind must not turn a successful copy into a failure.
    """
    if not path:
        return
    staging = os.path.dirname(os.path.abspath(path))
    # Only ever inside our own staging root — never follow a path out of it.
    if os.path.dirname(staging) == os.path.abspath(_TEMP_MEDIA_DIR):
        shutil.rmtree(staging, ignore_errors=True)
        return
    try:
        os.remove(path)
    except OSError as e:
        logger.debug("Could not remove temp file %s: %s", path, e)


def cleanup_temp_media() -> int:
    """
    Drop whatever the emergency download path left behind. Returns dirs removed.

    The per-send cleanup runs in a `finally`, which covers a failed send but not
    a killed process — so a crash mid-download used to leave gigabytes parked in
    the temp directory forever. Called once at worker startup, when no download
    can be in flight.
    """
    if not os.path.isdir(_TEMP_MEDIA_DIR):
        return 0
    removed = 0
    for name in os.listdir(_TEMP_MEDIA_DIR):
        target = os.path.join(_TEMP_MEDIA_DIR, name)
        try:
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                os.remove(target)
            removed += 1
        except OSError as e:
            logger.debug("Could not remove stale temp entry %s: %s", target, e)
    if removed:
        logger.info("Cleared %d stale staged download(s) from %s", removed, _TEMP_MEDIA_DIR)
    return removed


def _utf16_len(text: str) -> int:
    """
    Caption length the way Telegram counts it: UTF-16 code units, not characters.

    An emoji outside the BMP is one Python character but two units, so counting
    characters let a caption of 1024 emoji through and Telegram rejected it with
    MEDIA_CAPTION_TOO_LONG anyway.
    """
    return len(text.encode("utf-16-le")) // 2


def _raw_text(msg: Message) -> str:
    """
    A message's text without markup.

    Telethon's .text is the message re-rendered in the client's parse mode, so it
    carries markdown delimiters that .message does not. Those delimiters are not
    part of what Telegram counts against the caption limit, and on the raw-API
    album path they are not parsed either — they would land in the destination as
    literal '**'. .message is the field as Telegram stores it.
    """
    return getattr(msg, "message", None) or getattr(msg, "text", None) or ""


def _truncate_caption(text: Optional[str]) -> str:
    """
    Cut a caption down to what Telegram accepts on a fresh upload.

    Only for paths that re-upload the media. A forward keeps the original text
    however long it is, so it must never be routed through here.
    """
    text = text or ""
    if _utf16_len(text) <= _MAX_CAPTION_LEN:
        return text
    # Trim by code units, backing off a character at a time so a surrogate pair is
    # never cut in half — half a pair is not valid text and Telegram rejects it.
    out = text[: _MAX_CAPTION_LEN - 1]
    while out and _utf16_len(out) > _MAX_CAPTION_LEN - 1:
        out = out[:-1]
    return out + "…"

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
