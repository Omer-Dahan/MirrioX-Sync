"""CRUD for userbot accounts. Each userbot owns one Telethon session file."""
from __future__ import annotations

import logging
import re
from typing import Optional

from app import db
from app.models import Userbot

logger = logging.getLogger(__name__)

# Directory holding every userbot session file.
SESSION_DIR = "sessions"


def _slugify_phone(phone: str) -> str:
    """Turn a phone number into a filename-safe suffix."""
    return re.sub(r"[^0-9]", "", phone or "") or "account"


def build_session_name(phone: str) -> str:
    """
    Return a unique session path for a new account, e.g. 'sessions/userbot_972501234567'.
    Appends a numeric suffix if that path is already taken.
    """
    base = f"{SESSION_DIR}/userbot_{_slugify_phone(phone)}"
    candidate = base
    n = 1
    while get_by_session_name(candidate) is not None:
        n += 1
        candidate = f"{base}_{n}"
    return candidate


def create(
    name: str,
    phone: str,
    session_name: str,
    telegram_id: Optional[int] = None,
    username: Optional[str] = None,
    status: str = "active",
    is_default: bool = False,
) -> Userbot:
    conn = db.get_connection()
    cur = conn.execute(
        """INSERT INTO userbots
           (name, phone, session_name, telegram_id, username, status, is_default)
           VALUES (?,?,?,?,?,?,?)""",
        (name, phone, session_name, telegram_id, username, status, 1 if is_default else 0),
    )
    conn.commit()
    return get_by_id(cur.lastrowid)  # type: ignore[arg-type]


def get_by_id(userbot_id: int) -> Optional[Userbot]:
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM userbots WHERE id = ?", (userbot_id,)).fetchone()
    return Userbot.from_row(row) if row else None


def get_by_session_name(session_name: str) -> Optional[Userbot]:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM userbots WHERE session_name = ?", (session_name,)
    ).fetchone()
    return Userbot.from_row(row) if row else None


def get_by_phone(phone: str) -> Optional[Userbot]:
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM userbots WHERE phone = ?", (phone,)).fetchone()
    return Userbot.from_row(row) if row else None


def get_by_telegram_id(telegram_id: int) -> Optional[Userbot]:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM userbots WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    return Userbot.from_row(row) if row else None


def get_all() -> list[Userbot]:
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT * FROM userbots ORDER BY is_default DESC, id ASC"
    ).fetchall()
    return [Userbot.from_row(r) for r in rows]


def get_active() -> list[Userbot]:
    """Userbots eligible to run jobs. Default account first, then by id."""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT * FROM userbots WHERE status = 'active' ORDER BY is_default DESC, id ASC"
    ).fetchall()
    return [Userbot.from_row(r) for r in rows]


def count_active() -> int:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM userbots WHERE status = 'active'"
    ).fetchone()
    return row["cnt"] if row else 0


def set_status(userbot_id: int, status: str, error: Optional[str] = None) -> None:
    conn = db.get_connection()
    conn.execute(
        "UPDATE userbots SET status = ?, error_message = ? WHERE id = ?",
        (status, error, userbot_id),
    )
    conn.commit()


def update_identity(
    userbot_id: int,
    telegram_id: Optional[int],
    username: Optional[str],
    name: Optional[str] = None,
) -> None:
    """Store the resolved Telegram identity after a successful sign-in."""
    conn = db.get_connection()
    if name:
        conn.execute(
            "UPDATE userbots SET telegram_id = ?, username = ?, name = ? WHERE id = ?",
            (telegram_id, username, name, userbot_id),
        )
    else:
        conn.execute(
            "UPDATE userbots SET telegram_id = ?, username = ? WHERE id = ?",
            (telegram_id, username, userbot_id),
        )
    conn.commit()


def touch(userbot_id: int) -> None:
    """Record that this userbot is alive (heartbeat)."""
    conn = db.get_connection()
    conn.execute(
        "UPDATE userbots SET last_seen = datetime('now') WHERE id = ?", (userbot_id,)
    )
    conn.commit()


def delete(userbot_id: int) -> bool:
    conn = db.get_connection()
    cur = conn.execute(
        "DELETE FROM userbots WHERE id = ? AND is_default = 0", (userbot_id,)
    )
    conn.commit()
    return cur.rowcount > 0


def ensure_default(session_name: str, phone: str = "") -> Userbot:
    """
    Register the .env session as the default userbot if it isn't in the DB yet.
    Keeps single-account installs working with zero configuration. Idempotent.

    The row is usually created at startup before the phone number is known, so a
    phone supplied later (by `main.py setup`) backfills the empty field.
    """
    existing = get_by_session_name(session_name)
    if existing:
        if phone and not existing.phone:
            conn = db.get_connection()
            conn.execute(
                "UPDATE userbots SET phone = ? WHERE id = ?", (phone, existing.id)
            )
            conn.commit()
            existing = get_by_id(existing.id)  # type: ignore[assignment]
        _backfill_attribution_once(existing.id)
        return existing

    created = create(
        name="חשבון ראשי",
        phone=phone,
        session_name=session_name,
        status="active",
        is_default=True,
    )
    _backfill_attribution_once(created.id)
    return created


def _backfill_attribution_once(default_userbot_id: int) -> None:
    """
    Attribute the pre-multi-account backlog to the default account, once.

    Guarded by a settings flag rather than by "userbot_id IS NULL", because once
    several accounts are in play a NULL means "unknown" and must not be silently
    credited to the default account.
    """
    from app.repositories import state_repo

    if state_repo.get_setting("attribution_backfilled") == "1":
        return
    from app.repositories import job_repo

    n = job_repo.backfill_userbot_attribution(default_userbot_id)
    state_repo.set_setting("attribution_backfilled", "1")
    if n:
        logger.info(
            "Attributed %d historical message(s) to the default userbot (#%d)",
            n, default_userbot_id,
        )
