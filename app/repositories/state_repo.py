"""Worker state and app settings repositories."""
from __future__ import annotations

from typing import Optional
from app import db
from app.models import WorkerState


# ── Worker state ───────────────────────────────────────────────────────────────

def get_worker_state() -> WorkerState:
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM worker_state WHERE id = 1").fetchone()
    if row is None:
        # Safety: row should always exist after init_schema
        conn.execute("INSERT OR IGNORE INTO worker_state(id) VALUES(1)")
        conn.commit()
        row = conn.execute("SELECT * FROM worker_state WHERE id = 1").fetchone()
    return WorkerState.from_row(row)  # type: ignore[arg-type]


def set_worker_status(
    status: str,
    job_id: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    conn = db.get_connection()
    conn.execute(
        """UPDATE worker_state SET
             status = ?,
             current_job_id = ?,
             error_message = ?,
             last_heartbeat = datetime('now')
           WHERE id = 1""",
        (status, job_id, error),
    )
    conn.commit()


def heartbeat() -> None:
    conn = db.get_connection()
    conn.execute(
        "UPDATE worker_state SET last_heartbeat = datetime('now') WHERE id = 1"
    )
    conn.commit()


# ── App settings ───────────────────────────────────────────────────────────────

def get_setting(key: str) -> Optional[str]:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    conn = db.get_connection()
    conn.execute(
        """INSERT INTO app_settings(key, value, updated_at)
           VALUES(?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          updated_at = excluded.updated_at""",
        (key, value),
    )
    conn.commit()


def get_settings_dict() -> dict[str, str]:
    conn = db.get_connection()
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_int_setting(key: str, default: int) -> int:
    val = get_setting(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default
