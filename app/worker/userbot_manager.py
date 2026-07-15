"""
Multi-userbot supervision.

Every active row in `userbots` gets a UserbotRunner: its own Telethon client,
its own CopyEngine, and its own claim loop. Runners work different jobs at the
same time — parallelism is bounded by the number of active accounts.

Job hand-off rules:
  - Jobs are claimed atomically (`job_repo.claim_next_job`), so two accounts can
    never run the same job.
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
from app.models import Job, NoAccessError, Userbot
from app.network_errors import is_network_error
from app.repositories import job_repo, state_repo, userbot_repo, source_repo
from app.worker.copy_engine import CopyEngine

logger = logging.getLogger(__name__)

# How often each runner re-checks which continuous jobs it should be listening to.
_LISTENER_RECONCILE_EVERY_S = 30
# Safety bound so a bad DB state can never spin the claim loop.
_MAX_CONTINUOUS_CLAIMS_PER_CYCLE = 10
# Floor between channel access checks while a runner is busy copying, so the
# per-message callback can't turn into a query per message.
_CHANNEL_CHECK_EVERY_S = 10


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
        """Hand this account's continuous jobs back so another account resumes them."""
        released = job_repo.release_continuous_jobs(self.userbot.id)
        if released:
            logger.info(
                "Userbot %s: released %d continuous job(s) for another account to take over",
                self.label, released,
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

                # An account that has spent its daily quota stops taking work —
                # it must never park a job that an account with budget could run.
                if not self._is_capped():
                    job = job_repo.claim_next_job(self.userbot.id)
                    if job is not None:
                        await self._run_claimed_job(job)
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

    async def _handle_no_access(self, job: Job, reason: str) -> None:
        """
        This account can't reach the job's channels. Exclude it and put the job
        back so another account can try. Fail only when nobody is left.
        """
        excluded = job_repo.exclude_userbot(job.id, self.userbot.id)
        active_ids = {u.id for u in userbot_repo.get_active()}
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
                # Belt and braces: the runner releases its own continuous jobs on
                # the way out, but a hard crash could skip that.
                job_repo.release_continuous_jobs(ub_id)

        # Stop runners whose account is no longer active.
        for ub_id in list(self._tasks):
            if ub_id not in active_ids:
                logger.info("Userbot #%d deactivated — cancelling runner", ub_id)
                self._tasks[ub_id].cancel()
                self._tasks.pop(ub_id, None)
                self._runners.pop(ub_id, None)
                self.mark_idle(ub_id)
                job_repo.release_continuous_jobs(ub_id)

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
