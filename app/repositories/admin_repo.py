"""CRUD operations for the admins table."""
from __future__ import annotations

from typing import Optional
from app import db
from app.models import Admin


def get_all() -> list[Admin]:
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT * FROM admins ORDER BY added_at ASC"
    ).fetchall()
    return [Admin.from_row(r) for r in rows]


def get_by_telegram_id(telegram_id: int) -> Optional[Admin]:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM admins WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    return Admin.from_row(row) if row else None


def is_admin(telegram_id: int) -> bool:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT 1 FROM admins WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    return row is not None


def add(telegram_id: int, username: Optional[str], added_by: Optional[int]) -> Admin:
    conn = db.get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO admins(telegram_id, username, added_by) VALUES(?,?,?)",
        (telegram_id, username, added_by),
    )
    conn.commit()
    return get_by_telegram_id(telegram_id)  # type: ignore[return-value]


def update_username(telegram_id: int, username: Optional[str]) -> None:
    conn = db.get_connection()
    conn.execute(
        "UPDATE admins SET username = ? WHERE telegram_id = ?",
        (username, telegram_id),
    )
    conn.commit()


def remove(telegram_id: int) -> bool:
    conn = db.get_connection()
    cur = conn.execute(
        "DELETE FROM admins WHERE telegram_id = ?", (telegram_id,)
    )
    conn.commit()
    return cur.rowcount > 0
