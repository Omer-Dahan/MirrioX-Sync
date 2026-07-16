"""
Multi-userbot supervision.

Every active row in `userbots` gets a UserbotRunner: its own Telethon client,
its own CopyEngine, and its own claim loop. Runners work different jobs at the
same time — parallelism is bounded by the number of active accounts.

Job hand-off rules:
  - Jobs are claimed atomically (`job_repo.claim_next_job`), so two accounts can
    never lead the same job.
  - A job whose channels two or more accounts can reach is split into chunks by
    its leader. Any account that finds nothing new in the queue joins a running
    job by claiming a free chunk (`job_chunk_repo.claim_any`), so one job is
    copied by several accounts at once. New work in the queue is preferred over
    joining, which keeps whole jobs — and their message order — intact whenever
    there is enough work to go round.
  - If a runner cannot reach a job's channels it raises NoAccessError; the job is
    released back to the queue and that account is excluded from it, so a
    different account picks it up. Only when *every* active account has been
    excluded does the job fail.
  - The primary runner (the default account) additionally owns the singleton
    duties: duplicate scans and bulk deletes.
  - Channel access is checked by every runner for its own account, because access
    is a per-account fact: one account may be a member of a channel while another
    is not. The results feed the per-account access report in the bot UI.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from app.config import Config
from app.models import Job, JobChunk, NoAccessError, Userbot
from app.network_errors import is_network_error
from app.repositories import job_repo, job_chunk_repo, state_repo, userbot_repo, source_repo, hyper_repo
from app.worker.copy_engine import CopyEngine

logger = logging.getLogger(__name__)

# How often each runner re-checks which continuous jobs it should be listening to.
_LISTENER_RECONCILE_EVERY_S = 30
# Safety bound so a bad DB state can never spin the claim loop.
_MAX_CONTINUOUS_CLAIMS_PER_CYCLE = 10
# Floor between channel access checks while a runner is busy copying, so the
# per-message callback can't turn into a query per message.
_CHANNEL_CHECK_EVERY_S = 10
# How many parked hyper backups a runner drains per poll cycle, and how many
# times a single item is retried before it is given up on.
_HYPER_DRAIN_PER_CYCLE = 20
_HYPER_MAX_ATTEMPTS = 5


def build_client(config: Config, session_name: str) -> TelegramClient:
    """Create a Telethon client for one userbot session, with the worker's tuning."""
    session_dir = os.path.dirname(session_name)
    if session_dir:
        os.makedirs(session_dir, exist_ok=True)
    return TelegramClient(
        session_name,
        config.TELETHON_API_ID,
        config.TELETHON_API_HASH,
        flood_sleep_threshold=0,   # always raise FloodWaitError so we can log and requeue
        connection_retries=-1,     # retry indefinitely on network drops
        retry_delay=2,
    )


class UserbotRunner:
    """Owns one userbot account: its client, its engine, and its job loop."""

    def __init__(
        self,
        userbot: Userbot,
        config: Config,
        shutdown_event: asyncio.Event,
        manager: "UserbotManager",
        is_primary: bool = False,
    ) -> None:
        self.userbot = userbot
        self.config = config
        self._shutdown = shutdown_event
        self._manager = manager
        self.is_primary = is_primary
        self.client: TelegramClient | None = None
        self.engine: CopyEngine | None = None
        self._listeners: dict[int, object] = {}   # job_id → Telethon handler
        # Hyper backup: one global outgoing-message listener, pinned to this
        # account. Tracks the destination it is bound to so a config change
        # re-registers it and the loop-guard knows which chat is the backup.
        self._hyper_handler: object | None = None
        self._hyper_dest_id: int | None = None       # destinations.id currently backed up to
        self._hyper_guard_tgid: int | None = None     # backup channel's Telegram id (loop-guard)
        self._live_lock = asyncio.Lock()          # serialises live sends on this account
        self._last_reconcile = 0.0
        self._last_channel_check = 0.0
        self._capped_logged = False

    @property
    def id(self) -> int:
        return self.userbot.id

    @property
    def label(self) -> str:
        return f"{self.userbot.display()} (#{self.userbot.id})"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Connect and verify authorisation. Marks the account unauthorized on failure."""
        self.client = build_client(self.config, self.userbot.session_name)
        try:
            await self.client.connect()
        except Exception as e:
            logger.error("Userbot %s: connect failed: %s", self.label, e)
            userbot_repo.set_status(self.userbot.id, "error", str(e)[:300])
            return False

        if not await self.client.is_user_authorized():
            logger.error(
                "Userbot %s: session is not authorized — marking unauthorized. "
                "Re-add the account from the bot settings.",
                self.label,
            )
            userbot_repo.set_status(
                self.userbot.id, "unauthorized", "ה-session אינו מאושר — יש להוסיף את החשבון מחדש"
            )
            await self.disconnect()
            return False

        try:
            me = await self.client.get_me()
            userbot_repo.update_identity(
                self.userbot.id,
                getattr(me, "id", None),
                getattr(me, "username", None),
                name=self.userbot.name or getattr(me, "first_name", None),
            )
        except Exception:  # nosec B110 — identity refresh is best-effort
            pass

        userbot_repo.touch(self.userbot.id)
        self.engine = CopyEngine(
            self.client,
            resolve_callback=self._resolve_if_needed,
            userbot_id=self.userbot.id,
        )
        logger.info("Userbot %s: connected%s", self.label, " [primary]" if self.is_primary else "")
        return True

    async def disconnect(self) -> None:
        await self._remove_all_listeners()
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # nosec B110 — best-effort disconnect
                pass

    async def _resolve_if_needed(self) -> None:
        """Passed to CopyEngine: keep checking channels even while this account is busy."""
        await self._check_channels()

    async def _check_channels(self, force: bool = False) -> None:
        """Record this account's access to every channel it hasn't probed yet."""
        from app.worker import worker_main

        if self._manager.consume_resolve_trigger():
            force = True
        now = asyncio.get_running_loop().time()
        if not force and now - self._last_channel_check < _CHANNEL_CHECK_EVERY_S:
            return
        self._last_channel_check = now
        try:
            await worker_main.check_channels_for_account(self.client, self.userbot.id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Userbot %s: channel access check failed: %s", self.label, e)

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        if not await self.connect():
            # connect() may have failed after the account was already registered as
            # the owner of continuous jobs on a previous run — hand them back.
            self._release_continuous()
            return
        try:
            await self._loop()
        finally:
            # Synchronous DB work first: if this runner is being cancelled, the
            # await below can raise CancelledError and skip anything after it.
            self._release_continuous()
            await self.disconnect()
            logger.info("Userbot %s: stopped", self.label)

    def _release_continuous(self) -> None:
        """Hand this account's continuous jobs and chunks back to the other accounts."""
        released = job_repo.release_continuous_jobs(self.userbot.id)
        if released:
            logger.info(
                "Userbot %s: released %d continuous job(s) for another account to take over",
                self.label, released,
            )
        chunks = job_chunk_repo.release_for_userbot(self.userbot.id)
        if chunks:
            logger.info(
                "Userbot %s: released %d job chunk(s) back to the queue", self.label, chunks
            )

    async def _loop(self) -> None:
        poll_interval = self.config.WORKER_POLL_INTERVAL_S
        while not self._shutdown.is_set():
            try:
                if not await self._ensure_connected():
                    break

                # Refresh this account's state; it may have been disabled from the UI.
                fresh = userbot_repo.get_by_id(self.userbot.id)
                if fresh is None or fresh.status != "active":
                    logger.info("Userbot %s: no longer active — shutting runner down", self.label)
                    break
                self.userbot = fresh
                userbot_repo.touch(self.userbot.id)

                await self._reconcile_listeners()
                await self._reconcile_hyper()
                await self._drain_hyper_queue()

                # An account that has spent its daily quota stops taking work —
                # it must never park a job that an account with budget could run.
                if not self._is_capped():
                    job = job_repo.claim_next_job(self.userbot.id)
                    if job is not None:
                        await self._run_claimed_job(job)
                        continue

                    # Nothing new to lead. Join a job another account is already
                    # leading, if it was sharded — checked after claim_next_job so
                    # a queue with enough work for everyone still hands each
                    # account a whole job of its own.
                    joined = job_chunk_repo.claim_any(self.userbot.id)
                    if joined is not None:
                        await self._run_claimed_chunk(*joined)
                        continue

                if self.is_primary:
                    handled = await self._manager.run_primary_duties(self.client)
                    if handled:
                        continue

                # Idle: cheap enough to run every poll, so a channel added or
                # refreshed from the UI is re-checked by every account at once.
                await self._check_channels(force=True)
                await self._sleep(poll_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Userbot %s: poll loop error: %s", self.label, e)
                await self._sleep(10)

    def _is_capped(self) -> bool:
        """True while this account is out of daily quota. Logged once per transition."""
        from app.worker import worker_main

        capped = worker_main.account_is_capped(self.userbot.id)
        if capped != self._capped_logged:
            self._capped_logged = capped
            if capped:
                logger.info(
                    "Userbot %s: daily quota spent — not claiming until midnight "
                    "(other accounts keep working)", self.label,
                )
            else:
                logger.info("Userbot %s: daily quota reset — claiming again", self.label)
        return capped

    async def _ensure_connected(self) -> bool:
        if self.client is not None and self.client.is_connected():
            return True
        logger.warning("Userbot %s: disconnected — reconnecting...", self.label)
        return await self._reconnect()

    async def _reconnect(self) -> bool:
        """Reconnect with exponential backoff: 5 → 10 → 20 → 40 → 60s (capped)."""
        delay = 5
        attempt = 0
        while not self._shutdown.is_set():
            attempt += 1
            try:
                await self.client.connect()
                if await self.client.is_user_authorized():
                    logger.info("Userbot %s: reconnected (attempt #%d)", self.label, attempt)
                    # Event handlers live on the client, not the connection — Telethon
                    # keeps them across a reconnect. Dropping our references here would
                    # leave them registered and make the next reconcile add duplicates,
                    # so the existing listeners are deliberately left in place.
                    # Reconcile promptly to pick up job state that changed while offline.
                    self._last_reconcile = 0.0
                    return True
                logger.error("Userbot %s: session unauthorized after reconnect", self.label)
                userbot_repo.set_status(self.userbot.id, "unauthorized", "session אינו מאושר")
                return False
            except asyncio.CancelledError:
                return False
            except Exception as e:
                logger.warning(
                    "Userbot %s: reconnect #%d failed (%s) — retrying in %ds",
                    self.label, attempt, e, delay,
                )
                await self._sleep(delay)
                delay = min(delay * 2, 60)
        return False

    # ── Job execution ─────────────────────────────────────────────────────────

    async def _run_claimed_job(self, job: Job) -> None:
        logger.info(
            "Userbot %s: picked up job #%d '%s' (status=%s)",
            self.label, job.id, job.name, job.status,
        )
        self._manager.mark_busy(self.userbot.id, job.id)
        try:
            await self.engine.run_job(job)
            job_repo.clear_assignment(job.id, owner_id=self.userbot.id)
            await self._manager.notify_job_done(self.client, job.id)

        except NoAccessError as e:
            await self._handle_no_access(job, str(e))

        except FloodWaitError as e:
            await self._handle_flood_wait(job, e)

        except asyncio.CancelledError:
            job_repo.release_job(job.id, "pending")
            raise

        except Exception as e:
            await self._handle_job_error(job, e)

        finally:
            self._manager.mark_idle(self.userbot.id)

    async def _run_claimed_chunk(self, job: Job, chunk: JobChunk) -> None:
        """
        Copy one chunk of a job another account is leading.

        Every failure path here is deliberately narrower than _run_claimed_job's:
        the job belongs to its leader, which is copying its own chunks right now.
        A helper that hits trouble hands its chunk back and gets out of the way —
        pausing, failing or requeueing the job from here would yank it out from
        under an account that is working perfectly well. run_chunk has already
        released the chunk by the time any of this runs.
        """
        logger.info(
            "Userbot %s: joined job #%d '%s' on chunk #%d (ids %d–%d)",
            self.label, job.id, job.name, chunk.id, chunk.id_from, chunk.id_to,
        )
        self._manager.mark_busy(self.userbot.id, job.id)
        try:
            await self.engine.run_chunk(job, chunk)

        except NoAccessError as e:
            # Access is per-account: this one can't reach the channels even though
            # the leader can. Exclude it so it stops being offered the job's chunks.
            job_repo.exclude_userbot(job.id, self.userbot.id)
            logger.info(
                "Userbot %s: no access to job #%d (%s) — excluded from its chunks",
                self.label, job.id, e,
            )

        except FloodWaitError as e:
            # This account's flood wait, not the job's problem: the leader and the
            # other helpers keep going at full speed.
            logger.warning(
                "Userbot %s: FloodWait %ds on job #%d chunk #%d — backing off",
                self.label, e.seconds, job.id, chunk.id,
            )
            buf_min = state_repo.get_int_setting("flood_buffer_min_s", 5)
            buf_max = state_repo.get_int_setting("flood_buffer_max_s", 10)
            await self._sleep(e.seconds + random.uniform(buf_min, buf_max))  # nosec B311 — timing jitter

        except asyncio.CancelledError:
            job_chunk_repo.release(chunk.id, self.userbot.id)
            raise

        except Exception as e:
            if is_network_error(e):
                logger.warning(
                    "Userbot %s: network error on job #%d chunk #%d (%s) — reconnecting",
                    self.label, job.id, chunk.id, e,
                )
                await self._reconnect()
            else:
                logger.exception(
                    "Userbot %s: job #%d chunk #%d failed: %s", self.label, job.id, chunk.id, e
                )
            # The chunk is back in the queue either way. If it is genuinely broken
            # the leader will claim it in turn and fail the job through its own
            # retry path, which is the only place that decision belongs.
            await self._sleep(5)

        finally:
            self._manager.mark_idle(self.userbot.id)

    async def _handle_no_access(self, job: Job, reason: str) -> None:
        """
        This account can't reach the job's channels. Exclude it and put the job
        back so another account can try. Fail only when nobody is left.
        """
        excluded = job_repo.exclude_userbot(job.id, self.userbot.id)
        active_ids = {u.id for u in userbot_repo.get_active()}
        # A job restricted to specific accounts can only be run by those, so its
        # "anyone left to try?" question is asked within the allow-list — otherwise
        # excluding the last allowed account would leave the job stuck pending,
        # waiting on accounts that are barred from ever claiming it.
        allowed = job.allowed_ids()
        if allowed:
            active_ids &= allowed
        remaining = active_ids - excluded

        if not remaining:
            job_repo.update_status(
                job.id,
                "failed",
                error="אף חשבון יוזרבוט אינו חבר בערוץ המקור/היעד — הוסף את אחד החשבונות לערוץ ונסה שוב",
            )
            job_repo.clear_assignment(job.id, owner_id=self.userbot.id)
            logger.error(
                "Job #%d: no active userbot has access (tried %d account(s)) — failed",
                job.id, len(excluded),
            )
            await self._manager.notify_no_access(self.client, job.id, len(excluded))
        else:
            job_repo.release_job(job.id, "pending")
            logger.info(
                "Job #%d: userbot %s excluded (%s) — %d account(s) left to try",
                job.id, self.label, reason, len(remaining),
            )

    async def _handle_flood_wait(self, job: Job, e: FloodWaitError) -> None:
        wait_s = e.seconds
        buf_min = state_repo.get_int_setting("flood_buffer_min_s", 5)
        buf_max = state_repo.get_int_setting("flood_buffer_max_s", 10)
        buffer_s = random.uniform(buf_min, buf_max)  # nosec B311 — timing jitter, not crypto
        total_wait = wait_s + buffer_s
        retry_at = (datetime.utcnow() + timedelta(seconds=total_wait)).strftime("%Y-%m-%d %H:%M:%S")

        new_count = job_repo.increment_retry(job.id)
        max_retries = state_repo.get_int_setting("max_retries", 5)
        logger.warning(
            "Userbot %s job #%d: FloodWait %ds (buffer=%.1fs) — retry #%d/%d after %s",
            self.label, job.id, wait_s, buffer_s, new_count, max_retries, retry_at,
        )

        if new_count >= max_retries:
            job_repo.update_status(
                job.id,
                "paused",
                error=f"FloodWait: הגיע למקסימום ניסיונות ({max_retries}) — המשימה הושהתה, ניתן להמשיך ידנית",
            )
            job_repo.clear_assignment(job.id, owner_id=self.userbot.id)
            await self._manager.notify_disruption(
                self.client, job.id,
                f"FloodWait — הגיע למקסימום ניסיונות ({max_retries}). המשימה הושהתה — לחץ 'המשך' כדי להמשיך.",
            )
        else:
            # next_retry_at gates the queue, so no account touches it until it expires.
            job_repo.update_status(
                job.id, "waiting_retry", error=f"FloodWait {wait_s}s", next_retry_at=retry_at
            )
            job_repo.clear_assignment(job.id, owner_id=self.userbot.id)

        await self._sleep(min(total_wait, 60))

    async def _handle_job_error(self, job: Job, e: Exception) -> None:
        if is_network_error(e):
            logger.warning(
                "Userbot %s job #%d: network error (%s) — requeueing and reconnecting",
                self.label, job.id, e,
            )
            job_repo.release_job(job.id, "pending")
            await self._manager.notify_disruption(
                self.client, job.id, str(e)[:200], reconnecting=True
            )
            if await self._reconnect():
                await self._manager.notify_disruption(
                    self.client, job.id, str(e)[:200], resumed=True
                )
            await self._sleep(3)
            return

        logger.exception("Userbot %s job #%d: unexpected error: %s", self.label, job.id, e)
        new_count = job_repo.increment_retry(job.id)
        max_retries = state_repo.get_int_setting("max_retries", 5)

        if new_count >= max_retries:
            job_repo.update_status(job.id, "failed", error=str(e)[:500])
            job_repo.clear_assignment(job.id, owner_id=self.userbot.id)
            logger.error("Job #%d: max retries reached, marking failed", job.id)
        else:
            backoff_s = min(60 * (2 ** (new_count - 1)), 600)
            retry_at = (datetime.utcnow() + timedelta(seconds=backoff_s)).strftime("%Y-%m-%d %H:%M:%S")
            logger.warning(
                "Job #%d: retry #%d/%d — backoff %ds, resumes at %s",
                job.id, new_count, max_retries, backoff_s, retry_at,
            )
            job_repo.update_status(
                job.id, "waiting_retry", error=str(e)[:500], next_retry_at=retry_at
            )
            job_repo.clear_assignment(job.id, owner_id=self.userbot.id)
        await self._sleep(5)

    # ── Continuous ("always listening") jobs ──────────────────────────────────

    async def _reconcile_listeners(self) -> None:
        """
        Keep this account's real-time listeners in sync with the DB:
        claim new continuous jobs, register missing listeners, drop stale ones.
        """
        now = asyncio.get_running_loop().time()
        if now - self._last_reconcile < _LISTENER_RECONCILE_EVERY_S and self._listeners:
            return
        self._last_reconcile = now

        # Claim any continuous job not yet owned by an account. A capped account
        # takes no new listeners — an account with budget should get them instead.
        if not self._is_capped():
            for _ in range(_MAX_CONTINUOUS_CLAIMS_PER_CYCLE):
                claimed = job_repo.claim_continuous_job(self.userbot.id)
                if claimed is None:
                    break
                logger.info(
                    "Userbot %s: claimed continuous job #%d '%s'",
                    self.label, claimed.id, claimed.name,
                )

        active_jobs = job_repo.get_continuous_jobs_for(self.userbot.id)
        active_ids = {j.id for j in active_jobs}

        # Drop listeners for jobs that were paused, cancelled or reassigned.
        for job_id in list(self._listeners):
            if job_id not in active_ids:
                self._remove_listener(job_id)

        for job in active_jobs:
            if job.id not in self._listeners:
                await self._register_listener(job)

    async def _register_listener(self, job: Job) -> None:
        """Attach a real-time NewMessage handler for one continuous job."""
        from app.worker.telegram_utils import get_entity_safe

        src_rec = source_repo.get_source_by_id(job.source_id)
        if src_rec is None:
            job_repo.update_status(job.id, "failed", error="מקור לא נמצא")
            job_repo.clear_assignment(job.id, owner_id=self.userbot.id)
            return

        try:
            src_entity = await get_entity_safe(
                self.client, str(src_rec.resolved_id or src_rec.channel_ref)
            )
        except Exception as e:
            logger.warning(
                "Userbot %s: cannot listen to job #%d source (%s) — reassigning",
                self.label, job.id, e,
            )
            await self._handle_no_access(job, str(e))
            return

        handler = self._make_live_handler(job.id)
        self.client.add_event_handler(handler, events.NewMessage(chats=[src_entity]))
        self._listeners[job.id] = handler
        logger.info(
            "Userbot %s: listening on job #%d '%s' (source=%s)",
            self.label, job.id, job.name, src_rec.display(),
        )

    def _make_live_handler(self, job_id: int):
        async def _handler(event) -> None:
            job = job_repo.get_by_id(job_id)
            if job is None or not job.continuous or job.status != "running":
                return
            if job.assigned_userbot_id != self.userbot.id:
                return
            async with self._live_lock:
                try:
                    await self.engine.handle_live_message(job, event.message)
                except NoAccessError as e:
                    await self._handle_no_access(job, str(e))
                    self._remove_listener(job_id)
                except FloodWaitError as e:
                    logger.warning(
                        "Job #%d (continuous): FloodWait %ds on live message — retrying once",
                        job_id, e.seconds,
                    )
                    await self._sleep(min(e.seconds + 5, 120))
                    try:
                        await self.engine.handle_live_message(job, event.message)
                    except Exception as retry_err:
                        logger.warning(
                            "Job #%d (continuous): live retry failed: %s", job_id, retry_err
                        )
                except Exception as e:
                    logger.exception("Job #%d (continuous): live message failed: %s", job_id, e)

        return _handler

    def _remove_listener(self, job_id: int) -> None:
        handler = self._listeners.pop(job_id, None)
        if handler is None or self.client is None:
            return
        try:
            self.client.remove_event_handler(handler)
            logger.info("Userbot %s: stopped listening on job #%d", self.label, job_id)
        except Exception:  # nosec B110 — handler may already be gone
            pass

    async def _remove_all_listeners(self) -> None:
        for job_id in list(self._listeners):
            self._remove_listener(job_id)
        self._remove_hyper_handler()

    # ── Hyper backup (per-account outgoing capture) ────────────────────────────

    async def _reconcile_hyper(self) -> None:
        """
        Keep this account's hyper backup listener in sync with its config.

        Hyper is pinned to this account: it captures *this* account's outgoing
        messages, so it never migrates to another account the way continuous
        jobs do. Registering needs a reachable backup channel — its Telegram id
        is the loop-guard that stops us backing up our own backup.
        """
        from app.worker.telegram_utils import get_entity_safe

        cfg = hyper_repo.get_config(self.userbot.id)
        if not (cfg and cfg["enabled"] and cfg["destination_id"]):
            self._remove_hyper_handler()
            return

        # Already listening to this exact destination — nothing to do (and no
        # entity resolution on the hot path).
        if self._hyper_handler is not None and self._hyper_dest_id == cfg["destination_id"]:
            return

        dst_rec = source_repo.get_destination_by_id(cfg["destination_id"])
        if dst_rec is None:
            self._remove_hyper_handler()
            return

        try:
            dst_entity = await get_entity_safe(
                self.client, str(dst_rec.resolved_id or dst_rec.channel_ref)
            )
        except Exception as e:  # noqa: BLE001 — backup channel not reachable yet; retry next cycle
            logger.warning(
                "Userbot %s: hyper backup channel unreachable (%s) — will retry", self.label, e
            )
            self._remove_hyper_handler()
            return

        if not dst_rec.resolved_id:
            try:
                source_repo.update_destination_resolved(
                    dst_rec.id, getattr(dst_entity, "title", dst_rec.channel_ref), dst_entity.id
                )
            except Exception:  # nosec B110 — best-effort cache update
                pass

        self._remove_hyper_handler()
        # event.chat_id is a *marked* peer id (negative for channels), so the
        # loop-guard must compare against the marked id, not the raw entity.id.
        from telethon import utils as _tl_utils
        guard_tgid = _tl_utils.get_peer_id(dst_entity)
        handler = self._make_hyper_handler(dst_rec.id, guard_tgid)
        self.client.add_event_handler(handler, events.NewMessage(outgoing=True))
        self._hyper_handler = handler
        self._hyper_dest_id = dst_rec.id
        self._hyper_guard_tgid = guard_tgid
        logger.info(
            "Userbot %s: hyper backup ON → %s (dest #%d)",
            self.label, dst_rec.display(), dst_rec.id,
        )

    def _make_hyper_handler(self, dest_id: int, guard_tgid: int):
        async def _handler(event) -> None:
            # Loop-guard: our backup sends land in the backup channel as outgoing
            # messages too — never back those up, or every file loops forever.
            if event.chat_id == guard_tgid:
                return
            cfg = hyper_repo.get_config(self.userbot.id)
            if not cfg or not cfg["enabled"] or cfg["destination_id"] != dest_id:
                return
            dst_rec = source_repo.get_destination_by_id(dest_id)
            if dst_rec is None:
                return
            rules = hyper_repo.get_filters(self.userbot.id)
            chat_id, message_id = event.chat_id, event.message.id
            async with self._live_lock:
                try:
                    # Passing _is_capped defers (returns 'queued') instead of sending
                    # when out of quota, so a busy/capped moment never loses an upload.
                    status = await self.engine.handle_hyper_message(
                        dst_rec, event.message, rules, is_capped=self._is_capped
                    )
                except FloodWaitError as e:
                    logger.warning(
                        "Userbot %s: hyper FloodWait %ds — queuing for later", self.label, e.seconds
                    )
                    hyper_repo.enqueue(self.userbot.id, chat_id, message_id, dest_id)
                    await self._sleep(min(e.seconds + 5, 120))
                    return
                except Exception as e:  # noqa: BLE001
                    logger.exception("Userbot %s: hyper message failed: %s", self.label, e)
                    hyper_repo.enqueue(self.userbot.id, chat_id, message_id, dest_id)
                    hyper_repo.add_progress(self.userbot.id, failed=1)
                    return
            self._apply_hyper_live_status(status, chat_id, message_id, dest_id)

        return _handler

    def _apply_hyper_live_status(self, status: str, chat_id: int, message_id: int, dest_id: int) -> None:
        """Record a live hyper result: count it, and park it if it couldn't be sent yet."""
        if status == "copied":
            hyper_repo.add_progress(self.userbot.id, copied=1)
        elif status == "skipped":
            hyper_repo.add_progress(self.userbot.id, skipped=1)
        elif status == "queued":
            hyper_repo.enqueue(self.userbot.id, chat_id, message_id, dest_id)
            logger.info(
                "Userbot %s: hyper parked (daily cap) — will send when quota resets", self.label
            )
        elif status == "failed":
            hyper_repo.enqueue(self.userbot.id, chat_id, message_id, dest_id)
            hyper_repo.add_progress(self.userbot.id, failed=1)

    async def _drain_hyper_queue(self) -> None:
        """
        Send parked hyper backups now that the account can send again.

        Re-fetches each queued message from Telegram (the media was never stored
        locally) and runs it through the same pipeline. Dedup makes a re-queued
        item that was meanwhile sent by another account a no-op. Bounded per cycle
        so it interleaves with normal claiming instead of blocking it.
        """
        cfg = hyper_repo.get_config(self.userbot.id)
        if not (cfg and cfg["enabled"] and cfg["destination_id"]):
            return
        if self._is_capped() or hyper_repo.queue_count(self.userbot.id) == 0:
            return
        dst_rec = source_repo.get_destination_by_id(cfg["destination_id"])
        if dst_rec is None:
            return
        rules = hyper_repo.get_filters(self.userbot.id)

        for _ in range(_HYPER_DRAIN_PER_CYCLE):
            if self._is_capped():
                break
            rows = hyper_repo.dequeue_batch(self.userbot.id, 1)
            if not rows:
                break
            row = rows[0]

            try:
                msg = await self.client.get_messages(row["chat_id"], ids=row["message_id"])
            except Exception as e:  # noqa: BLE001 — transient fetch problem, keep and retry
                logger.debug("Userbot %s: hyper drain fetch failed (%s)", self.label, e)
                if hyper_repo.queue_bump_attempts(row["id"]) >= _HYPER_MAX_ATTEMPTS:
                    hyper_repo.queue_remove(row["id"])
                continue
            if msg is None:
                hyper_repo.queue_remove(row["id"])  # source message deleted — nothing to back up
                continue

            async with self._live_lock:
                try:
                    status = await self.engine.handle_hyper_message(
                        dst_rec, msg, rules, is_capped=self._is_capped
                    )
                except FloodWaitError as e:
                    await self._sleep(min(e.seconds + 5, 120))
                    break
                except Exception as e:  # noqa: BLE001
                    logger.warning("Userbot %s: hyper drain send failed: %s", self.label, e)
                    status = "failed"

            if status == "copied":
                hyper_repo.queue_remove(row["id"])
                hyper_repo.add_progress(self.userbot.id, copied=1)
            elif status == "skipped":
                hyper_repo.queue_remove(row["id"])
                hyper_repo.add_progress(self.userbot.id, skipped=1)
            elif status == "queued":
                break  # became capped mid-drain — leave the rest for next time
            else:  # failed
                if hyper_repo.queue_bump_attempts(row["id"]) >= _HYPER_MAX_ATTEMPTS:
                    hyper_repo.queue_remove(row["id"])
                    hyper_repo.add_progress(self.userbot.id, failed=1)

    def _remove_hyper_handler(self) -> None:
        if self._hyper_handler is None:
            return
        try:
            if self.client is not None:
                self.client.remove_event_handler(self._hyper_handler)
        except Exception:  # nosec B110 — handler may already be gone
            pass
        self._hyper_handler = None
        self._hyper_dest_id = None
        self._hyper_guard_tgid = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


class UserbotManager:
    """Supervises one runner per active userbot and reconciles as accounts change."""

    def __init__(self, config: Config, shutdown_event: asyncio.Event) -> None:
        self.config = config
        self._shutdown = shutdown_event
        self._runners: dict[int, UserbotRunner] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._busy: dict[int, int] = {}  # userbot_id → job_id
        self._resolve_trigger: asyncio.Event | None = None

    def set_resolve_trigger(self, event: asyncio.Event) -> None:
        self._resolve_trigger = event

    # ── Worker state aggregation ──────────────────────────────────────────────

    def mark_busy(self, userbot_id: int, job_id: int) -> None:
        self._busy[userbot_id] = job_id
        self._sync_worker_state()

    def mark_idle(self, userbot_id: int) -> None:
        self._busy.pop(userbot_id, None)
        self._sync_worker_state()

    def _sync_worker_state(self) -> None:
        """worker_state is a single row — report the first busy job, else idle."""
        if self._busy:
            first_job = next(iter(self._busy.values()))
            state_repo.set_worker_status("running", job_id=first_job)
        else:
            state_repo.set_worker_status("idle")

    # ── Primary-only duties (delegated back to worker_main) ───────────────────

    def consume_resolve_trigger(self) -> bool:
        """
        True once after the bot asks for an immediate channel check.

        Only an accelerator: the checks are driven by the DB, so a runner that
        misses the trigger still picks the work up on its next cycle.
        """
        if self._resolve_trigger is not None and self._resolve_trigger.is_set():
            self._resolve_trigger.clear()
            return True
        return False

    async def run_primary_duties(self, client: TelegramClient) -> bool:
        """Scans and bulk deletes. True if real work happened."""
        from app.worker import worker_main
        return await worker_main.run_primary_duties(client)

    async def notify_job_done(self, client: TelegramClient, job_id: int) -> None:
        from app.worker import worker_main
        await worker_main.send_completion_notification(client, job_id)

    async def notify_disruption(
        self, client: TelegramClient, job_id: int, msg: str, **kwargs
    ) -> None:
        from app.worker import worker_main
        await worker_main.send_network_disruption_notification(client, job_id, msg, **kwargs)

    async def notify_no_access(self, client: TelegramClient, job_id: int, tried: int) -> None:
        from app.worker import worker_main
        await worker_main.send_no_access_notification(client, job_id, tried)

    # ── Supervision ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start runners for all active accounts and keep the set in sync."""
        while not self._shutdown.is_set():
            try:
                await self._reconcile_runners()
                if not self._tasks:
                    logger.warning(
                        "No active userbot accounts — worker idle. "
                        "Add an account from the bot settings (⚙️ הגדרות → 🤖 חשבונות יוזרבוט)."
                    )
                    state_repo.set_worker_status("idle")
                await self._sleep(_LISTENER_RECONCILE_EVERY_S)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Userbot manager error: %s", e)
                await self._sleep(10)
        await self._stop_all()

    async def _reconcile_runners(self) -> None:
        """Start runners for newly added accounts; reap finished ones."""
        active = userbot_repo.get_active()
        active_ids = {u.id for u in active}

        # Reap tasks that exited (disabled account, unauthorized session, crash).
        for ub_id in list(self._tasks):
            task = self._tasks[ub_id]
            if task.done():
                exc = task.exception() if not task.cancelled() else None
                if exc:
                    logger.error("Userbot #%d runner crashed: %s", ub_id, exc)
                self._tasks.pop(ub_id, None)
                self._runners.pop(ub_id, None)
                self.mark_idle(ub_id)
                # Belt and braces: the runner releases its own continuous jobs and
                # chunks on the way out, but a hard crash could skip that.
                job_repo.release_continuous_jobs(ub_id)
                job_chunk_repo.release_for_userbot(ub_id)

        # Stop runners whose account is no longer active.
        for ub_id in list(self._tasks):
            if ub_id not in active_ids:
                logger.info("Userbot #%d deactivated — cancelling runner", ub_id)
                self._tasks[ub_id].cancel()
                self._tasks.pop(ub_id, None)
                self._runners.pop(ub_id, None)
                self.mark_idle(ub_id)
                job_repo.release_continuous_jobs(ub_id)
                job_chunk_repo.release_for_userbot(ub_id)

        # Start runners for accounts that don't have one yet.
        primary_id = active[0].id if active else None
        for ub in active:
            if ub.id in self._tasks:
                continue
            runner = UserbotRunner(
                userbot=ub,
                config=self.config,
                shutdown_event=self._shutdown,
                manager=self,
                is_primary=(ub.id == primary_id),
            )
            self._runners[ub.id] = runner
            self._tasks[ub.id] = asyncio.create_task(runner.run(), name=f"userbot-{ub.id}")
            logger.info("Started runner for userbot %s", runner.label)

        # Re-point the primary role every cycle. Runners already running keep the
        # flag they were constructed with, so without this the primary duties
        # (scans, deletes, heartbeat, channel resolution) would stop for good the
        # moment the original primary account was disabled or lost its session.
        self._apply_primary_role(primary_id)

    def _apply_primary_role(self, primary_id: int | None) -> None:
        for ub_id, runner in self._runners.items():
            should_be_primary = (ub_id == primary_id)
            if runner.is_primary != should_be_primary:
                runner.is_primary = should_be_primary
                logger.info(
                    "Userbot %s: %s the primary role (scans, deletes, channel resolution)",
                    runner.label, "took over" if should_be_primary else "handed off",
                )

    async def _stop_all(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        for task in self._tasks.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):  # nosec B110 — shutdown best-effort
                pass
        self._tasks.clear()
        self._runners.clear()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
