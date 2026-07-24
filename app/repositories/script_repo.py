"""Repository for the global script library and the per-userbot ad-hoc task queue.

Two concerns live here:
- `scripts`      — a reusable library of Python snippets (like Linux aliases).
- `userbot_tasks`— the run queue/history. The bot enqueues a row; the target
  account's runner claims and runs it against its live client. A task is pinned
  to a single userbot, so claiming needs no cross-account race handling.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.db import get_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Script library
# ---------------------------------------------------------------------------

def create_script(name: str, code: str) -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO scripts(name, code) VALUES (?, ?)", (name, code)
    )
    conn.commit()
    return cur.lastrowid


def list_scripts() -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM scripts ORDER BY name COLLATE NOCASE ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_script(script_id: int) -> Optional[dict[str, Any]]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
    return dict(row) if row else None


def get_script_by_name(name: str) -> Optional[dict[str, Any]]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM scripts WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def update_script(script_id: int, name: str, code: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE scripts SET name = ?, code = ?, updated_at = datetime('now') WHERE id = ?",
        (name, code, script_id),
    )
    conn.commit()


def delete_script(script_id: int) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM scripts WHERE id = ?", (script_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Ad-hoc task queue
# ---------------------------------------------------------------------------

def enqueue_task(
    userbot_id: int, code: str, chat_id: Optional[int], script_id: Optional[int] = None
) -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO userbot_tasks(userbot_id, script_id, code, chat_id) VALUES (?,?,?,?)",
        (userbot_id, script_id, code, chat_id),
    )
    conn.commit()
    return cur.lastrowid


def claim_next_task(userbot_id: int) -> Optional[dict[str, Any]]:
    """
    Atomically claim the oldest pending task for this account.

    Two-step SELECT-then-guarded-UPDATE, mirroring job_repo.claim_next_job. A task
    is pinned to one userbot, so the guard only protects against this same runner
    racing itself across poll cycles, never against another account.
    """
    conn = get_connection()
    row = conn.execute(
        """SELECT id FROM userbot_tasks
           WHERE userbot_id = ? AND status = 'pending'
           ORDER BY id ASC LIMIT 1""",
        (userbot_id,),
    ).fetchone()
    if row is None:
        return None

    cur = conn.execute(
        """UPDATE userbot_tasks
           SET status = 'running', started_at = datetime('now')
           WHERE id = ? AND status = 'pending'""",
        (row["id"],),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_task(row["id"])


def finish_task(task_id: int, status: str, output: Optional[str]) -> None:
    conn = get_connection()
    conn.execute(
        """UPDATE userbot_tasks
           SET status = ?, output = ?, finished_at = datetime('now')
           WHERE id = ?""",
        (status, output, task_id),
    )
    conn.commit()


def get_task(task_id: int) -> Optional[dict[str, Any]]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM userbot_tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_recent_tasks(userbot_id: int, limit: int = 10) -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM userbot_tasks WHERE userbot_id = ? ORDER BY id DESC LIMIT ?",
        (userbot_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def fail_pending_tasks(userbot_id: int, reason: str) -> int:
    """
    Mark an account's not-yet-run tasks as 'error' (called on remove/disable).

    A pending task is pinned to one account; if that account is removed or turned
    off it will never be claimed, so the row would sit 'pending' forever — or, on a
    later re-enable, run long after the admin asked for it. Better to close it out
    with a visible reason.
    """
    conn = get_connection()
    cur = conn.execute(
        """UPDATE userbot_tasks
           SET status = 'error', output = ?, finished_at = datetime('now')
           WHERE userbot_id = ? AND status = 'pending'""",
        (reason, userbot_id),
    )
    conn.commit()
    return cur.rowcount


def reset_running_tasks_to_error(reason: str) -> int:
    """
    Mark any task stuck in 'running' as 'error' on startup.

    A snippet may have side effects (sends, deletes), so re-running an interrupted
    task could duplicate them. It is safer to report the interruption than to retry.
    """
    conn = get_connection()
    cur = conn.execute(
        """UPDATE userbot_tasks
           SET status = 'error', output = ?, finished_at = datetime('now')
           WHERE status = 'running'""",
        (reason,),
    )
    conn.commit()
    return cur.rowcount
