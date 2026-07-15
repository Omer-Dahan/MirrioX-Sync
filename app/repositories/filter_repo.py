"""CRUD for the blocked_words table."""
from __future__ import annotations

from typing import Optional
from app import db
from app.models import BlockedWord


def get_all() -> list[BlockedWord]:
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT * FROM blocked_words ORDER BY word ASC"
    ).fetchall()
    return [BlockedWord.from_row(r) for r in rows]


def get_word_strings() -> list[str]:
    """Return just the word text values (lowercase)."""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT word FROM blocked_words ORDER BY word ASC"
    ).fetchall()
    return [r["word"].lower() for r in rows]


def get_by_id(word_id: int) -> Optional[BlockedWord]:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM blocked_words WHERE id = ?", (word_id,)
    ).fetchone()
    return BlockedWord.from_row(row) if row else None


def add_word(word: str, added_by: Optional[int] = None) -> BlockedWord:
    conn = db.get_connection()
    cur = conn.execute(
        "INSERT OR IGNORE INTO blocked_words(word, added_by) VALUES(?,?)",
        (word.strip(), added_by),
    )
    conn.commit()
    # If the word already existed, fetch it by text
    if cur.lastrowid:
        row = conn.execute(
            "SELECT * FROM blocked_words WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM blocked_words WHERE word = ? COLLATE NOCASE",
            (word.strip(),),
        ).fetchone()
    return BlockedWord.from_row(row)  # type: ignore[arg-type]


def remove_by_id(word_id: int) -> bool:
    conn = db.get_connection()
    cur = conn.execute(
        "DELETE FROM blocked_words WHERE id = ?", (word_id,)
    )
    conn.commit()
    return cur.rowcount > 0


def remove_by_text(word: str) -> bool:
    conn = db.get_connection()
    cur = conn.execute(
        "DELETE FROM blocked_words WHERE word = ? COLLATE NOCASE", (word,)
    )
    conn.commit()
    return cur.rowcount > 0


def clear_all() -> int:
    """Delete all blocked words. Returns number removed."""
    conn = db.get_connection()
    cur = conn.execute("DELETE FROM blocked_words")
    conn.commit()
    return cur.rowcount


def count() -> int:
    conn = db.get_connection()
    row = conn.execute("SELECT COUNT(*) as cnt FROM blocked_words").fetchone()
    return row["cnt"] if row else 0
