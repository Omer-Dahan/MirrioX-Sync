"""Conservative rate limiter with batch pauses, dynamic FloodWait, and throughput tracking."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque

logger = logging.getLogger(__name__)

# Adaptive slowdown applied after a FloodWait. Telegram just told us the current
# pace is too fast, so going straight back to it is how one FloodWait turns into
# five: every delay is multiplied by this factor, and the multiplier decays back
# to 1.0 only after a long clean stretch.
_SLOWDOWN_STEP = 1.5
_SLOWDOWN_MAX = 4.0
_SLOWDOWN_DECAY = 1.25
# Messages that must go out without a FloodWait before one decay step is applied.
_SLOWDOWN_DECAY_AFTER = 150

# An album is one API call but N messages in the channel, and Telegram's limits
# count messages. Charging it a flat 2x delay is what made album-heavy jobs run
# 4-5x faster than the configured pace. Each extra item now costs this fraction
# of a full per-message delay — less than a solo send (it really is one call),
# far more than nothing.
_ALBUM_ITEM_FACTOR = 0.6
_ALBUM_MIN_FACTOR = 2.0


class _DestinationGate:
    """
    Process-wide minimum spacing between sends to one destination channel.

    Every account has its own RateLimiter, so "2-5 seconds between messages" was
    only ever true per account: three accounts sharding one job into one channel
    delivered three times that rate, and the channel — not the account — is what
    Telegram flood-limited. This gate is shared by all the runners in the worker
    process, so the aggregate rate into a destination stays bounded no matter how
    many accounts are working it.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_free = 0.0

    async def reserve(self, seconds: float) -> None:
        """Claim the next `seconds` of this destination's budget, then wait for it."""
        if seconds <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next_free)
            self._next_free = start + seconds
            wait = start - now
        if wait > 0:
            await asyncio.sleep(wait)


_dest_gates: dict[int, _DestinationGate] = {}


def _gate_for(dest_id: int) -> _DestinationGate:
    gate = _dest_gates.get(dest_id)
    if gate is None:
        gate = _DestinationGate()
        _dest_gates[dest_id] = gate
    return gate


class LabeledAdapter(logging.LoggerAdapter):
    """Prefixes every log line with the owning account's label."""

    def process(self, msg, kwargs):
        return f"[{self.extra['label']}] {msg}", kwargs


class RateLimiter:
    """
    Enforces per-message random delays and periodic batch pauses.
    Tracks hourly/daily throughput for logging.
    """

    def __init__(
        self,
        min_ms: int = 2000,
        max_ms: int = 5000,
        flood_buffer_min_s: int = 5,
        flood_buffer_max_s: int = 10,
        batch_size_min: int = 50,
        batch_size_max: int = 100,
        batch_pause_min_s: int = 60,
        batch_pause_max_s: int = 120,
        dest_min_delay_ms: int = 1000,
        label: str | None = None,
    ):
        self._log = LabeledAdapter(logger, {"label": label}) if label else logger
        self.min_ms = min_ms
        self.max_ms = max_ms
        self.flood_buffer_min_s = flood_buffer_min_s
        self.flood_buffer_max_s = flood_buffer_max_s
        self.batch_size_min = batch_size_min
        self.batch_size_max = batch_size_max
        self.batch_pause_min_s = batch_pause_min_s
        self.batch_pause_max_s = batch_pause_max_s
        self.dest_min_delay_ms = dest_min_delay_ms

        # Messages, not calls: an album is a single request to Telegram but ten
        # messages in the channel, and the batch pause exists to bound what the
        # channel receives. Counting calls made the pause fire every 500-1000
        # messages on an album-heavy job instead of every 50-100.
        self._send_count: int = 0
        self._next_batch_pause_at: int = self._new_batch_threshold()
        # Sliding window for throughput reporting, one entry per *message* — an
        # album of ten counts as ten, which is what actually reached the channel.
        self._sent_timestamps: deque[float] = deque()
        # Adaptive pacing: >1.0 while recovering from a FloodWait.
        self._slowdown: float = 1.0
        self._sends_since_flood: int = 0

    def _new_batch_threshold(self) -> int:
        return self._send_count + random.randint(self.batch_size_min, self.batch_size_max)  # nosec B311

    def update_from_settings(self, settings: dict[str, str]) -> None:
        try:
            self.min_ms              = int(settings.get("min_delay_ms",        self.min_ms))
            self.max_ms              = int(settings.get("max_delay_ms",        self.max_ms))
            self.flood_buffer_min_s  = int(settings.get("flood_buffer_min_s",  self.flood_buffer_min_s))
            self.flood_buffer_max_s  = int(settings.get("flood_buffer_max_s",  self.flood_buffer_max_s))
            self.batch_size_min      = int(settings.get("batch_size_min",      self.batch_size_min))
            self.batch_size_max      = int(settings.get("batch_size_max",      self.batch_size_max))
            self.batch_pause_min_s   = int(settings.get("batch_pause_min_s",   self.batch_pause_min_s))
            self.batch_pause_max_s   = int(settings.get("batch_pause_max_s",   self.batch_pause_max_s))
            self.dest_min_delay_ms   = int(settings.get("dest_min_delay_ms",   self.dest_min_delay_ms))
            if self.min_ms > self.max_ms:
                self.min_ms, self.max_ms = self.max_ms, self.min_ms
            if self.flood_buffer_min_s > self.flood_buffer_max_s:
                self.flood_buffer_min_s, self.flood_buffer_max_s = (
                    self.flood_buffer_max_s, self.flood_buffer_min_s
                )
        except (ValueError, TypeError):
            pass

    async def wait(
        self, album: bool = False, count: int = 1, dest_id: int | None = None
    ) -> None:
        """Random per-message delay, followed by a batch pause when the threshold is hit.
        Pass album=True after sending a media group — the delay scales with the number of
        items, since that is what the destination channel actually received.
        Pass count=<items> so throughput and the batch counter reflect messages delivered.
        Pass dest_id so the send is also spaced against every other account writing
        to the same destination channel."""
        count = max(1, count)
        now = time.monotonic()
        self._sent_timestamps.extend([now] * count)
        self._send_count += count
        self._note_clean_sends(count)

        delay_s = random.uniform(self.min_ms / 1000.0, self.max_ms / 1000.0)  # nosec B311
        if album and count > 1:
            factor = max(_ALBUM_MIN_FACTOR, 1.0 + (count - 1) * _ALBUM_ITEM_FACTOR)
            delay_s *= factor
            self._log.debug("Album delay: %.1fs (%d items, %.1fx)", delay_s, count, factor)
        delay_s *= self._slowdown
        await asyncio.sleep(delay_s)

        # The per-account delay above says nothing about what the *channel* is
        # receiving from the other accounts, so the shared gate is charged on top.
        if dest_id is not None and self.dest_min_delay_ms > 0:
            await _gate_for(dest_id).reserve(self.dest_min_delay_ms / 1000.0 * count)

        if self._send_count >= self._next_batch_pause_at:
            pause_s = random.uniform(self.batch_pause_min_s, self.batch_pause_max_s)  # nosec B311
            self._log.info(
                "Batch pause (%d messages since start) — sleeping %.0fs before continuing",
                self._send_count, pause_s,
            )
            self._log_throughput()
            await asyncio.sleep(pause_s)
            self._next_batch_pause_at = self._new_batch_threshold()

    async def handle_flood_wait(self, seconds: int) -> None:
        """Sleep for the Telegram-required time plus a random jitter buffer, and slow down."""
        self.note_flood_wait(seconds)
        buffer = random.uniform(self.flood_buffer_min_s, self.flood_buffer_max_s)  # nosec B311
        total = seconds + buffer
        self._log.warning(
            "FloodWait: sleeping %.1fs  (telegram=%ds + jitter=%.1fs, pace now %.2fx slower)",
            total, seconds, buffer, self._slowdown,
        )
        await asyncio.sleep(total)

    def note_flood_wait(self, seconds: int) -> None:
        """Register a FloodWait without sleeping — raises the adaptive slowdown."""
        self._slowdown = min(self._slowdown * _SLOWDOWN_STEP, _SLOWDOWN_MAX)
        self._sends_since_flood = 0
        self._log.debug("FloodWait %ds noted — slowdown now %.2fx", seconds, self._slowdown)

    def _note_clean_sends(self, count: int) -> None:
        """Decay the slowdown one step per clean stretch, so the pace recovers gradually."""
        if self._slowdown <= 1.0:
            return
        self._sends_since_flood += count
        if self._sends_since_flood >= _SLOWDOWN_DECAY_AFTER:
            self._sends_since_flood = 0
            self._slowdown = max(1.0, self._slowdown / _SLOWDOWN_DECAY)
            self._log.info("Pace recovering — slowdown now %.2fx", self._slowdown)

    def log_flood_wait(self, seconds: int, retry_count: int) -> None:
        """Log a FloodWait event (call before requeueing the job)."""
        self._log.warning(
            "FloodWait %ds received (retry #%d). Job will resume after backoff.",
            seconds, retry_count,
        )

    def _log_throughput(self) -> None:
        """Prune the sliding window and log msgs/hour and msgs/24h."""
        now = time.monotonic()
        day_ago = now - 86400
        hour_ago = now - 3600

        while self._sent_timestamps and self._sent_timestamps[0] < day_ago:
            self._sent_timestamps.popleft()

        last_hour = sum(1 for t in self._sent_timestamps if t >= hour_ago)
        last_day = len(self._sent_timestamps)
        self._log.info(
            "Throughput: %d msgs/last-hour | %d msgs/this-session (resets on restart)",
            last_hour, last_day,
        )
