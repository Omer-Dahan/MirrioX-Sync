"""Conservative rate limiter with batch pauses, dynamic FloodWait, and throughput tracking."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque

logger = logging.getLogger(__name__)


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
    ):
        self.min_ms = min_ms
        self.max_ms = max_ms
        self.flood_buffer_min_s = flood_buffer_min_s
        self.flood_buffer_max_s = flood_buffer_max_s
        self.batch_size_min = batch_size_min
        self.batch_size_max = batch_size_max
        self.batch_pause_min_s = batch_pause_min_s
        self.batch_pause_max_s = batch_pause_max_s

        self._msg_count: int = 0
        self._next_batch_pause_at: int = self._new_batch_threshold()
        # Sliding window of send timestamps for throughput reporting
        self._sent_timestamps: deque[float] = deque()

    def _new_batch_threshold(self) -> int:
        return self._msg_count + random.randint(self.batch_size_min, self.batch_size_max)  # nosec B311

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
            if self.min_ms > self.max_ms:
                self.min_ms, self.max_ms = self.max_ms, self.min_ms
            if self.flood_buffer_min_s > self.flood_buffer_max_s:
                self.flood_buffer_min_s, self.flood_buffer_max_s = (
                    self.flood_buffer_max_s, self.flood_buffer_min_s
                )
        except (ValueError, TypeError):
            pass

    async def wait(self, album: bool = False) -> None:
        """Random per-message delay, followed by a batch pause when the threshold is hit.
        Pass album=True after sending a media group — doubles the delay to reduce FloodWait risk."""
        now = time.monotonic()
        self._sent_timestamps.append(now)
        self._msg_count += 1

        delay_s = random.uniform(self.min_ms / 1000.0, self.max_ms / 1000.0)  # nosec B311
        if album:
            delay_s *= 2
            logger.debug("Album delay: %.1fs (2x normal)", delay_s)
        await asyncio.sleep(delay_s)

        if self._msg_count >= self._next_batch_pause_at:
            pause_s = random.uniform(self.batch_pause_min_s, self.batch_pause_max_s)  # nosec B311
            logger.info(
                "Batch pause after %d messages — sleeping %.0fs before continuing",
                self._msg_count, pause_s,
            )
            self._log_throughput()
            await asyncio.sleep(pause_s)
            self._next_batch_pause_at = self._new_batch_threshold()

    async def handle_flood_wait(self, seconds: int) -> None:
        """Sleep for the Telegram-required time plus a random jitter buffer."""
        buffer = random.uniform(self.flood_buffer_min_s, self.flood_buffer_max_s)  # nosec B311
        total = seconds + buffer
        logger.warning(
            "FloodWait: sleeping %.1fs  (telegram=%ds + jitter=%.1fs)",
            total, seconds, buffer,
        )
        await asyncio.sleep(total)

    def log_flood_wait(self, seconds: int, retry_count: int) -> None:
        """Log a FloodWait event (call before requeueing the job)."""
        logger.warning(
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
        logger.info(
            "Throughput: %d msgs/last-hour | %d msgs/this-session (resets on restart)",
            last_hour, last_day,
        )
