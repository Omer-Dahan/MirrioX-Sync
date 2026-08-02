"""
Parallel (multi-connection) file transfer for Telethon.

Telethon moves a file over a single MTProto connection, which caps a transfer at
roughly 0.5-1 MB/s no matter how much bandwidth is available. Telegram is happy
to serve the same file over many connections at once, so opening several senders
to the file's datacenter and striping the parts across them is worth 10-20x on a
normal link. That is the whole reason the public "save restricted content" bots
feel instant: a protected channel *must* be downloaded and re-uploaded (Telegram
rejects a file_reference from a protected chat outright — see copy_engine), and
this is what stops that from taking hours.

Adapted from `parallel_file_transfer.py` in mautrix-telegram,
Copyright (C) 2021 Tulir Asokan, MIT licensed:
https://github.com/tulir/mautrix-telegram/blob/master/mautrix_telegram/util/parallel_file_transfer.py
(via https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6)

Changes from upstream, all for running unattended inside a worker:
  - Progress callbacks dropped — nobody is watching a progress bar here.
  - Connection count is capped by the caller, not fixed at 20: five accounts each
    opening twenty sockets from one IP is how you earn a FloodWait.
  - One transfer at a time per client, so a single account cannot multiply its
    own connection count by running two transfers at once.
  - Documents only. A photo is small enough that the single-connection path costs
    nothing, and `Photo` has no `.size` for the part maths to work from.

This leans on Telethon internals (`client._call`, `client._get_dc`,
`client._connection`, `client._init_request`). They are verified against the
pinned telethon==1.37.0; `is_available()` re-checks them at runtime and every
caller must be able to fall back to the ordinary path — see `copy_engine`.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import time
from collections import defaultdict
from typing import AsyncGenerator, BinaryIO, Optional, Union

from telethon import TelegramClient, helpers, utils
from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import (
    ExportAuthorizationRequest,
    ImportAuthorizationRequest,
)
from telethon.tl.functions.upload import (
    GetFileRequest,
    SaveBigFilePartRequest,
    SaveFilePartRequest,
)
from telethon.tl.types import Document, InputFile, InputFileBig, TypeInputFile

logger = logging.getLogger(__name__)

# Telegram's own threshold for the "big file" upload API.
_BIG_FILE_BYTES = 10 * 1024 * 1024
# File size at which the full connection budget is used. Below it the count is
# scaled down — a 5MB file has nothing to gain from twenty sockets.
_FULL_SPEED_BYTES = 100 * 1024 * 1024
# Below this the parallel path is not used at all. Every transfer opens its
# sockets and tears them down again afterwards, so a small file pays two rounds
# of connection setup to save a fraction of a second — and that churn, repeated
# once per message, is itself something Telegram notices. This is a floor only:
# `can_transfer` also checks that the file actually resolves to more than one
# connection at the caller's connection count, since `connection_count` scales
# down below `_FULL_SPEED_BYTES` and a file just over this floor can still land
# on a single connection — all of the setup cost above for none of the benefit.
_MIN_PARALLEL_BYTES = 8 * 1024 * 1024
# How long a per-part FloodWait may be absorbed in place, and how many times.
# Telegram's transfer throttle for a non-premium account arrives as a one- or
# two-second wait on a single part; anything longer is a real limit and belongs
# to the caller's own backoff.
_MAX_INLINE_FLOOD_S = 20
_FLOOD_ATTEMPTS = 4

# One parallel transfer at a time per client, so an account cannot double its own
# socket count by transferring two files at once.
_client_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# Ceiling on transfers running at the same time across *all* accounts. Only a
# protected source needs a transfer at all, and there is usually one of those —
# so in practice this never binds. It exists for the case that is easy to walk
# into by accident: add five protected sources and every account starts a
# transfer at once, turning a sane per-transfer count into a per-IP problem.
# The connection count is the knob to turn; this is the guard rail behind it.
_MAX_CONCURRENT_TRANSFERS = 2
_transfer_slots: Optional[asyncio.Semaphore] = None


def transfer_lock(client: TelegramClient) -> asyncio.Lock:
    """The per-account lock guarding parallel transfers. Held for a whole file."""
    return _client_locks[id(client)]


def transfer_slot() -> asyncio.Semaphore:
    """
    The process-wide transfer permit. Created lazily so it binds to the running loop.

    Total sockets in flight are bounded by
    `_MAX_CONCURRENT_TRANSFERS × parallel_connections`, regardless of how many
    accounts or protected sources exist.
    """
    global _transfer_slots
    if _transfer_slots is None:
        _transfer_slots = asyncio.Semaphore(_MAX_CONCURRENT_TRANSFERS)
    return _transfer_slots


def is_available(client: TelegramClient) -> bool:
    """
    True if this Telethon build still exposes everything the fast path needs.

    Checked rather than assumed: the internals below are not public API, so a
    Telethon upgrade is allowed to take them away. When it does, callers fall
    back to the ordinary single-connection path instead of failing the copy.
    """
    return all(
        getattr(client, name, None) is not None
        for name in ("_call", "_get_dc", "_connection", "_init_request", "_log")
    )


def can_transfer(media, max_count: int) -> bool:
    """
    True if this media is a document the parallel path can carry, and would
    actually be split across more than one connection at `max_count`.

    The size floor alone doesn't guarantee that: `connection_count` scales
    down for files under `_FULL_SPEED_BYTES`, so without this check a file
    just over `_MIN_PARALLEL_BYTES` could still resolve to a single
    connection — the parallel path's setup overhead for a single-connection
    result, which is strictly worse than not using it at all.
    """
    doc = getattr(media, "document", None)
    if not isinstance(doc, Document) or doc.size < _MIN_PARALLEL_BYTES:
        return False
    return _ParallelTransferrer.connection_count(doc.size, max_count) >= 2


async def _call_absorbing_floods(client: TelegramClient, sender: MTProtoSender, request):
    """
    Send one part's request, sleeping through the small waits Telegram hands out.

    Telegram meters a non-premium account's transfer rate and says so with a
    one- or two-second FloodWait on a single part — `A wait of 2 seconds is
    required in non-premium accounts`. That is throttling, not an error: it
    means "this part, a moment later". Letting it out aborted the whole file and
    took fifteen sibling parts down with it, so it is waited out here instead.
    A long wait, or one that keeps coming back, still goes up to the caller.
    """
    attempts = 0
    while True:
        try:
            return await client._call(sender, request)
        except FloodWaitError as e:
            attempts += 1
            if e.seconds > _MAX_INLINE_FLOOD_S or attempts > _FLOOD_ATTEMPTS:
                raise
            await asyncio.sleep(e.seconds + 1)


class _DownloadSender:
    """One connection's share of a download: every `stride` bytes, `count` times."""

    def __init__(
        self, client: TelegramClient, sender: MTProtoSender, file,
        offset: int, limit: int, stride: int, count: int,
    ) -> None:
        self.client = client
        self.sender = sender
        self.request = GetFileRequest(file, offset=offset, limit=limit)
        self.stride = stride
        self.remaining = count

    async def next(self) -> Optional[bytes]:
        if not self.remaining:
            return None
        result = await _call_absorbing_floods(self.client, self.sender, self.request)
        self.remaining -= 1
        self.request.offset += self.stride
        return result.bytes

    def disconnect(self):
        return self.sender.disconnect()


class _UploadSender:
    """One connection's share of an upload, pipelined one part deep."""

    def __init__(
        self, client: TelegramClient, sender: MTProtoSender, file_id: int,
        part_count: int, big: bool, index: int, stride: int,
    ) -> None:
        self.client = client
        self.sender = sender
        self.part_count = part_count
        self.request: Union[SaveFilePartRequest, SaveBigFilePartRequest] = (
            SaveBigFilePartRequest(file_id, index, part_count, b"")
            if big else SaveFilePartRequest(file_id, index, b"")
        )
        self.stride = stride
        self.previous: Optional[asyncio.Task] = None

    async def next(self, data: bytes) -> None:
        # Wait for this sender's previous part before queueing the next, so the
        # parts of one connection stay in order while the connections race.
        if self.previous:
            await self.previous
        self.previous = asyncio.get_running_loop().create_task(self._next(data))

    async def _next(self, data: bytes) -> None:
        self.request.bytes = data
        await _call_absorbing_floods(self.client, self.sender, self.request)
        self.request.file_part += self.stride

    async def disconnect(self) -> None:
        if self.previous:
            await self.previous
        await self.sender.disconnect()


class _ParallelTransferrer:
    """Owns the extra senders for one file, and closes them when it is done."""

    def __init__(self, client: TelegramClient, dc_id: Optional[int] = None) -> None:
        self.client = client
        self.dc_id = dc_id or client.session.dc_id
        # Only reuse the session's key when the file lives on the session's own
        # DC; anywhere else needs an exported authorisation.
        self.auth_key: Optional[AuthKey] = (
            None if dc_id and client.session.dc_id != dc_id else client.session.auth_key
        )
        self.senders: Optional[list] = None

    @staticmethod
    def connection_count(file_size: int, max_count: int) -> int:
        if file_size > _FULL_SPEED_BYTES:
            return max_count
        return max(1, math.ceil((file_size / _FULL_SPEED_BYTES) * max_count))

    async def _create_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(self.client._connection(
            dc.ip_address, dc.port, dc.id,
            loggers=self.client._log, proxy=self.client._proxy,
        ))
        if not self.auth_key:
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            self.client._init_request.query = ImportAuthorizationRequest(
                id=auth.id, bytes=auth.bytes
            )
            await sender.send(InvokeWithLayerRequest(LAYER, self.client._init_request))
            self.auth_key = sender.auth_key
        return sender

    async def _open_senders(self, count: int) -> list:
        """
        Open `count` connections: the first alone, the rest together.

        The first one has to go up by itself. On a cross-DC transfer it is the
        one that generates the auth key and exports the account's authorisation
        to that DC, and it caches the result — opening all of them at once would
        have every connection repeat the handshake and the login, and fight over
        the single `client._init_request` while doing it. Once the key exists the
        rest are just TCP connects, so they are made in parallel.
        """
        first = await self._create_sender()
        if count == 1:
            return [first]
        rest = await asyncio.gather(
            *[self._create_sender() for _ in range(count - 1)]
        )
        return [first, *rest]

    async def _cleanup(self) -> None:
        if not self.senders:
            return
        await asyncio.gather(
            *[s.disconnect() for s in self.senders], return_exceptions=True
        )
        self.senders = None

    # ── Download ──────────────────────────────────────────────────────────────

    async def download(
        self, file, file_size: int, connection_count: int
    ) -> AsyncGenerator[bytes, None]:
        part_size = utils.get_appropriated_part_size(file_size) * 1024
        part_count = math.ceil(file_size / part_size)
        minimum, remainder = divmod(part_count, connection_count)

        def share() -> int:
            nonlocal remainder
            if remainder > 0:
                remainder -= 1
                return minimum + 1
            return minimum

        # Evaluated before the connections open, so the split stays deterministic.
        shares = [share() for _ in range(connection_count)]
        try:
            senders = await self._open_senders(connection_count)
            self.senders = [
                _DownloadSender(
                    self.client, sender, file,
                    # Connection i starts at part i and then skips a whole round
                    # of connections each time, so the ranges interleave and no
                    # two of them ever ask for the same bytes.
                    i * part_size, part_size, connection_count * part_size, shares[i],
                )
                for i, sender in enumerate(senders)
            ]
            done = 0
            while done < part_count:
                loop = asyncio.get_running_loop()
                tasks = [loop.create_task(s.next()) for s in self.senders]
                # Collected before yielding, not while: the round's siblings have
                # to be settled inside this try, and awaiting them from a `finally`
                # that a generator may be closing is not something to rely on. It
                # buffers one round — connections × part_size — and no more.
                chunks: list[bytes] = []
                try:
                    for task in tasks:
                        data = await task
                        if not data:
                            break
                        chunks.append(data)
                finally:
                    # Every sibling must be accounted for. When one part raised
                    # and the rest were abandoned, asyncio logged "Task exception
                    # was never retrieved" for each of them — 163 tracebacks in
                    # one run, none of them the actual problem.
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

                for data in chunks:
                    yield data
                    done += 1
                if len(chunks) < len(tasks):
                    break  # a sender ran out of parts; the file is complete
        finally:
            await self._cleanup()

    # ── Upload ────────────────────────────────────────────────────────────────

    async def upload(
        self, stream: BinaryIO, file_size: int, file_id: int,
        connection_count: int, file_name: str,
    ) -> TypeInputFile:
        part_size = utils.get_appropriated_part_size(file_size) * 1024
        part_count = (file_size + part_size - 1) // part_size
        is_large = file_size > _BIG_FILE_BYTES
        # Telegram verifies the checksum on small uploads only; the big-file API
        # has no md5 field at all.
        md5 = hashlib.md5() if not is_large else None  # nosec B324 — Telegram's own checksum

        try:
            senders = await self._open_senders(connection_count)
            self.senders = [
                # Same interleaving as the download, but by part *index* rather
                # than byte offset: connection i sends part i, then i+N, i+2N…
                _UploadSender(
                    self.client, sender, file_id,
                    part_count, is_large, i, connection_count,
                )
                for i, sender in enumerate(senders)
            ]

            ticker = 0
            while True:
                part = stream.read(part_size)
                if not part:
                    break
                if md5 is not None:
                    md5.update(part)
                await self.senders[ticker].next(part)
                ticker = (ticker + 1) % len(self.senders)
        finally:
            await self._cleanup()

        if is_large:
            return InputFileBig(file_id, part_count, file_name)
        return InputFile(file_id, part_count, file_name, md5.hexdigest())


# ── Public API ────────────────────────────────────────────────────────────────

def _report(what: str, size: int, count: int, started: float) -> None:
    """
    Log what the transfer actually achieved.

    At INFO, because the throughput is the whole reason this module exists: it is
    the only way to tell whether the connection count is set anywhere near right,
    and it beats guessing from theory.
    """
    elapsed = max(time.monotonic() - started, 1e-6)
    mb = size / (1024 * 1024)
    logger.info(
        "%s %.1fMB in %.1fs over %d connection(s) — %.1f MB/s",
        what, mb, elapsed, count, mb / elapsed,
    )


async def download_document(
    client: TelegramClient, document: Document, path: str, max_connections: int
) -> None:
    """Download one document to `path` over several connections at once."""
    dc_id, location = utils.get_input_location(document)
    transferrer = _ParallelTransferrer(client, dc_id)
    count = _ParallelTransferrer.connection_count(document.size, max_connections)
    started = time.monotonic()
    async with transfer_slot():
        with open(path, "wb") as out:
            async for chunk in transferrer.download(location, document.size, count):
                out.write(chunk)
    _report(f"Downloaded from DC {dc_id}:", document.size, count, started)


async def upload_document(
    client: TelegramClient, path: str, max_connections: int
) -> TypeInputFile:
    """
    Upload a file from disk over several connections at once.

    Returns the handle to hand to `send_file`. Note that the raw handle carries
    no metadata of its own, so the caller must pass the source document's
    `attributes` along with it or a video arrives as a nameless document.
    """
    file_size = os.path.getsize(path)
    transferrer = _ParallelTransferrer(client)
    count = _ParallelTransferrer.connection_count(file_size, max_connections)
    started = time.monotonic()
    async with transfer_slot():
        with open(path, "rb") as stream:
            handle = await transferrer.upload(
                stream, file_size, helpers.generate_random_long(),
                count, os.path.basename(path),
            )
    _report("Uploaded", file_size, count, started)
    return handle
