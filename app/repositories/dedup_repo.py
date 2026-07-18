"""
Global registry of transferred content ("skip if already sent").

Dedup key:
  - media   → the Telegram media ID (photo.id / document.id). Stable across
              channels and accounts, same identifier the scan engine uses.
  - text    → sha1 of the normalised message text.

Scope is per-destination: sending the same content to a *different* channel is
not a duplicate. Rows are recorded on every successful transfer regardless of
the skip_duplicates setting, so enabling the setting later has full history.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from app import db

logger = logging.getLogger(__name__)


def compute_key(msg) -> Optional[tuple[str, str]]:
    """
    Return (kind, dedup_key) for a Telethon Message, or None if the message
    carries nothing worth deduplicating (empty / service messages).
    """
    from telethon.tl.types import MessageMediaUnsupported

    media = getattr(msg, "media", None)
    if media is not None and not isinstance(media, MessageMediaUnsupported):
        type_name = media.__class__.__name__
        if type_name == "MessageMediaPhoto":
            photo = getattr(media, "photo", None)
            if photo is not None and getattr(photo, "id", None) is not None:
                return "media", f"photo:{photo.id}"
        elif type_name == "MessageMediaDocument":
            doc = getattr(media, "document", None)
            if doc is not None and getattr(doc, "id", None) is not None:
                return "media", f"doc:{doc.id}"
        # Other media types (polls, geo, ...) are not deduplicated.
        return None

    text = (getattr(msg, "text", None) or "").strip()
    if not text:
        return None
    digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()  # nosec B324 — dedup key, not security
    return "text", f"text:{digest}"


def exists(destination_id: int, dedup_key: str) -> bool:
    """True if this content was already transferred to this destination."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT 1 FROM transferred_registry WHERE destination_id = ? AND dedup_key = ? LIMIT 1",
        (destination_id, dedup_key),
    ).fetchone()
    return row is not None


def get_existing_keys(destination_id: int, dedup_keys: list[str]) -> set[str]:
    """Batch variant of exists() — returns the subset of keys already present."""
    if not dedup_keys:
        return set()
    conn = db.get_connection()
    placeholders = ",".join("?" * len(dedup_keys))
    rows = conn.execute(
        f"SELECT dedup_key FROM transferred_registry "  # nosec B608 — placeholders are generated, not user input
        f"WHERE destination_id = ? AND dedup_key IN ({placeholders})",
        [destination_id, *dedup_keys],
    ).fetchall()
    return {r["dedup_key"] for r in rows}


def record(
    destination_id: int,
    kind: str,
    dedup_key: str,
    source_id: Optional[int] = None,
    source_message_id: Optional[int] = None,
    job_id: Optional[int] = None,
) -> None:
    """
    Record a transferred item. Idempotent — re-recording the same
    (destination, key) pair keeps the original row.
    """
    conn = db.get_connection()
    conn.execute(
        """INSERT INTO transferred_registry
           (dedup_key, kind, destination_id, source_id, source_message_id, job_id)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(destination_id, dedup_key) DO NOTHING""",
        (dedup_key, kind, destination_id, source_id, source_message_id, job_id),
    )
    conn.commit()


def record_message(
    msg,
    destination_id: int,
    source_id: Optional[int] = None,
    job_id: Optional[int] = None,
) -> None:
    """Compute the key for a message and record it. Never raises."""
    try:
        computed = compute_key(msg)
        if computed is None:
            return
        kind, key = computed
        record(
            destination_id=destination_id,
            kind=kind,
            dedup_key=key,
            source_id=source_id,
            source_message_id=getattr(msg, "id", None),
            job_id=job_id,
        )
    except Exception:  # nosec B110 — registry write must never break a transfer
        logger.debug("dedup record failed for msg %s", getattr(msg, "id", "?"), exc_info=True)


def is_duplicate(msg, destination_id: int) -> bool:
    """True if this message's content was already sent to this destination."""
    computed = compute_key(msg)
    if computed is None:
        return False
    return exists(destination_id, computed[1])


def exists_any(destination_ids: list[int], dedup_key: str) -> bool:
    """True if this key was already sent to any of the given destinations."""
    if not destination_ids:
        return False
    conn = db.get_connection()
    placeholders = ",".join("?" * len(destination_ids))
    row = conn.execute(
        "SELECT 1 FROM transferred_registry "  # nosec B608 — generated placeholders only
        f"WHERE destination_id IN ({placeholders}) AND dedup_key = ? LIMIT 1",
        [*destination_ids, dedup_key],
    ).fetchone()
    return row is not None


def is_duplicate_any(msg, destination_ids: list[int]) -> bool:
    """True if this message's content already reached any of a job's destinations."""
    computed = compute_key(msg)
    if computed is None:
        return False
    return exists_any(destination_ids, computed[1])


def count_for_destination(destination_id: int) -> int:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transferred_registry WHERE destination_id = ?",
        (destination_id,),
    ).fetchone()
    return row["cnt"] if row else 0


def count_all() -> int:
    conn = db.get_connection()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM transferred_registry").fetchone()
    return row["cnt"] if row else 0
