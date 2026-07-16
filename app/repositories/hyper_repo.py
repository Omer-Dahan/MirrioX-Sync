"""
Per-account hyper-backup configuration and smart filter rules.

Hyper is deliberately *not* a row in the jobs table: it has no single source, it
is pinned to one account (its own outgoing traffic), and its dedup is content
based rather than message-id based. So it lives in its own two tables.

  - hyper_configs: one row per userbot — enabled flag, backup destination, counters.
  - hyper_filters: one row per (userbot, media_type) — the size/duration bounds.
"""
from __future__ import annotations

from typing import Optional

from app import db
from app.services.hyper_filter import MEDIA_TYPES

# Numeric bound columns a caller may set — whitelisted so the column name can be
# interpolated into SQL safely.
_BOUND_FIELDS = frozenset({"min_size", "max_size", "min_duration", "max_duration"})


def _row_to_dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


# ── Config ─────────────────────────────────────────────────────────────────────

def ensure_config(userbot_id: int) -> None:
    """Create the config row and default (capture-all) filter rows if missing."""
    conn = db.get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO hyper_configs(userbot_id) VALUES(?)", (userbot_id,)
    )
    for media_type in MEDIA_TYPES:
        conn.execute(
            "INSERT OR IGNORE INTO hyper_filters(userbot_id, media_type) VALUES(?, ?)",
            (userbot_id, media_type),
        )
    conn.commit()


def get_config(userbot_id: int) -> Optional[dict]:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM hyper_configs WHERE userbot_id = ?", (userbot_id,)
    ).fetchone()
    return _row_to_dict(row)


def set_enabled(userbot_id: int, enabled: bool) -> None:
    ensure_config(userbot_id)
    conn = db.get_connection()
    conn.execute(
        "UPDATE hyper_configs SET enabled = ?, updated_at = datetime('now') WHERE userbot_id = ?",
        (1 if enabled else 0, userbot_id),
    )
    conn.commit()


def set_destination(userbot_id: int, destination_id: Optional[int]) -> None:
    ensure_config(userbot_id)
    conn = db.get_connection()
    conn.execute(
        "UPDATE hyper_configs SET destination_id = ?, updated_at = datetime('now') WHERE userbot_id = ?",
        (destination_id, userbot_id),
    )
    conn.commit()


def add_progress(userbot_id: int, copied: int = 0, skipped: int = 0, failed: int = 0) -> None:
    conn = db.get_connection()
    conn.execute(
        """UPDATE hyper_configs SET
             copied_count  = COALESCE(copied_count,0)  + ?,
             skipped_count = COALESCE(skipped_count,0) + ?,
             failed_count  = COALESCE(failed_count,0)  + ?,
             updated_at    = datetime('now')
           WHERE userbot_id = ?""",
        (copied, skipped, failed, userbot_id),
    )
    conn.commit()


def delete_config(userbot_id: int) -> None:
    """Remove an account's hyper config and rules (called when the account is deleted)."""
    conn = db.get_connection()
    conn.execute("DELETE FROM hyper_configs WHERE userbot_id = ?", (userbot_id,))
    conn.execute("DELETE FROM hyper_filters WHERE userbot_id = ?", (userbot_id,))
    conn.execute("DELETE FROM hyper_transfers WHERE userbot_id = ?", (userbot_id,))
    conn.execute("DELETE FROM hyper_queue WHERE userbot_id = ?", (userbot_id,))
    conn.commit()


def record_send(userbot_id: int) -> None:
    """
    Stamp one successful hyper transfer for the daily-cap counter.

    Kept separate from copied_messages on purpose — see hyper_transfers in the
    schema — but counted together with it by job_repo.get_daily_count_for_userbot,
    so hyper sends consume the same per-account daily quota as bulk copying.
    """
    conn = db.get_connection()
    conn.execute("INSERT INTO hyper_transfers(userbot_id) VALUES(?)", (userbot_id,))
    conn.commit()


# ── Pending queue (waiting for the account to be able to send) ──────────────────

def enqueue(userbot_id: int, chat_id: int, message_id: int, dest_id: int) -> None:
    """Queue a message to back up later. Idempotent per (account, chat, message, dest)."""
    conn = db.get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO hyper_queue(userbot_id, chat_id, message_id, dest_id) VALUES(?,?,?,?)",
        (userbot_id, chat_id, message_id, dest_id),
    )
    conn.commit()


def dequeue_batch(userbot_id: int, limit: int = 1) -> list:
    """Return the oldest queued items for this account (not removed — caller decides)."""
    conn = db.get_connection()
    return conn.execute(
        "SELECT * FROM hyper_queue WHERE userbot_id = ? ORDER BY id ASC LIMIT ?",
        (userbot_id, limit),
    ).fetchall()


def queue_remove(row_id: int) -> None:
    conn = db.get_connection()
    conn.execute("DELETE FROM hyper_queue WHERE id = ?", (row_id,))
    conn.commit()


def queue_bump_attempts(row_id: int) -> int:
    """Increment the retry counter for a queued item and return the new value."""
    conn = db.get_connection()
    conn.execute("UPDATE hyper_queue SET attempts = attempts + 1 WHERE id = ?", (row_id,))
    conn.commit()
    row = conn.execute("SELECT attempts FROM hyper_queue WHERE id = ?", (row_id,)).fetchone()
    return row["attempts"] if row else 999


def queue_count(userbot_id: int) -> int:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM hyper_queue WHERE userbot_id = ?", (userbot_id,)
    ).fetchone()
    return row["cnt"] if row else 0


def any_enabled() -> bool:
    """True if hyper backup is turned on for at least one account."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT 1 FROM hyper_configs WHERE enabled = 1 AND destination_id IS NOT NULL LIMIT 1"
    ).fetchone()
    return row is not None


# ── Filters ────────────────────────────────────────────────────────────────────

def get_filters(userbot_id: int) -> dict[str, dict]:
    """Return {media_type: rule dict} for this account, filling in any missing type."""
    ensure_config(userbot_id)
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT * FROM hyper_filters WHERE userbot_id = ?", (userbot_id,)
    ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        out[d["media_type"]] = d
    return out


def get_filter(userbot_id: int, media_type: str) -> Optional[dict]:
    return get_filters(userbot_id).get(media_type)


def toggle_type_enabled(userbot_id: int, media_type: str) -> None:
    ensure_config(userbot_id)
    conn = db.get_connection()
    conn.execute(
        "UPDATE hyper_filters SET enabled = 1 - enabled WHERE userbot_id = ? AND media_type = ?",
        (userbot_id, media_type),
    )
    conn.commit()


def set_combine(userbot_id: int, media_type: str, combine: str) -> None:
    if combine not in ("and", "or"):
        return
    ensure_config(userbot_id)
    conn = db.get_connection()
    conn.execute(
        "UPDATE hyper_filters SET combine = ? WHERE userbot_id = ? AND media_type = ?",
        (combine, userbot_id, media_type),
    )
    conn.commit()


def set_bound(userbot_id: int, media_type: str, field: str, value: Optional[int]) -> None:
    """Set (or clear, with value=None) one size/duration bound. Field is whitelisted."""
    if field not in _BOUND_FIELDS:
        raise ValueError(f"unknown hyper bound field: {field}")
    ensure_config(userbot_id)
    conn = db.get_connection()
    conn.execute(
        f"UPDATE hyper_filters SET {field} = ? "  # nosec B608 — field is whitelisted above
        "WHERE userbot_id = ? AND media_type = ?",
        (value, userbot_id, media_type),
    )
    conn.commit()
