"""
Per-userbot access results for source/destination channels.

Every active userbot probes every channel on its own, so the UI can report which
accounts can actually reach it. A missing row means "not checked yet by that
account"; the worker fills it in on its next idle cycle.
"""
from __future__ import annotations

from typing import Optional

from app import db
from app.models import ChannelAccessRow

KIND_SOURCE = "source"
KIND_DEST = "destination"


def record(
    channel_kind: str,
    channel_id: int,
    userbot_id: int,
    has_access: bool,
    error: str | None = None,
) -> None:
    """Store (or refresh) one account's access result for one channel."""
    conn = db.get_connection()
    conn.execute(
        """INSERT INTO channel_access
               (channel_kind, channel_id, userbot_id, has_access, error, checked_at)
           VALUES (?,?,?,?,?, datetime('now'))
           ON CONFLICT(channel_kind, channel_id, userbot_id) DO UPDATE SET
               has_access = excluded.has_access,
               error      = excluded.error,
               checked_at = excluded.checked_at""",
        (channel_kind, channel_id, userbot_id, 1 if has_access else 0, error),
    )
    conn.commit()


def get_unchecked_channels(userbot_id: int) -> list[tuple[str, int, str]]:
    """Channels this account hasn't probed yet, as (kind, channel_id, channel_ref)."""
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT 'source' AS kind, s.id AS id, s.channel_ref AS channel_ref
             FROM sources s
            WHERE NOT EXISTS (SELECT 1 FROM channel_access ca
                               WHERE ca.channel_kind = 'source'
                                 AND ca.channel_id = s.id
                                 AND ca.userbot_id = ?)
           UNION ALL
           SELECT 'destination', d.id, d.channel_ref
             FROM destinations d
            WHERE NOT EXISTS (SELECT 1 FROM channel_access ca
                               WHERE ca.channel_kind = 'destination'
                                 AND ca.channel_id = d.id
                                 AND ca.userbot_id = ?)""",
        (userbot_id, userbot_id),
    ).fetchall()
    return [(r["kind"], r["id"], r["channel_ref"]) for r in rows]


def get_report(channel_kind: str, channel_id: int) -> list[ChannelAccessRow]:
    """One row per active account: who can reach this channel, who can't, who is pending."""
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT u.id AS userbot_id, u.name, u.username, u.phone,
                  u.status AS userbot_status,
                  ca.has_access, ca.error, ca.checked_at
             FROM userbots u
             LEFT JOIN channel_access ca
                    ON ca.userbot_id = u.id
                   AND ca.channel_kind = ?
                   AND ca.channel_id = ?
            WHERE u.status = 'active'
            ORDER BY u.is_default DESC, u.id ASC""",
        (channel_kind, channel_id),
    ).fetchall()
    return [ChannelAccessRow.from_row(r) for r in rows]


def any_active_has_access(channel_kind: str, channel_id: int) -> bool:
    conn = db.get_connection()
    row = conn.execute(
        """SELECT 1 FROM channel_access ca
             JOIN userbots u ON u.id = ca.userbot_id
            WHERE ca.channel_kind = ? AND ca.channel_id = ?
              AND ca.has_access = 1 AND u.status = 'active'
            LIMIT 1""",
        (channel_kind, channel_id),
    ).fetchone()
    return row is not None


def active_with_access(source_id: int, destination_id: int) -> set[int]:
    """
    Active accounts proven to reach *both* channels of a job.

    This is what decides whether a job is worth splitting across accounts. Unlike
    the claim rules — which treat an unprobed channel as claimable so a lagging
    check can't stall the queue — this test is positive on purpose: sharding a job
    onto an account that turns out to have no access only costs a reassignment,
    and running unsharded is always correct.
    """
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT u.id AS userbot_id FROM userbots u
            WHERE u.status = 'active'
              AND EXISTS (SELECT 1 FROM channel_access ca
                           WHERE ca.userbot_id = u.id AND ca.has_access = 1
                             AND ca.channel_kind = 'source' AND ca.channel_id = ?)
              AND EXISTS (SELECT 1 FROM channel_access ca
                           WHERE ca.userbot_id = u.id AND ca.has_access = 1
                             AND ca.channel_kind = 'destination' AND ca.channel_id = ?)""",
        (source_id, destination_id),
    ).fetchall()
    return {r["userbot_id"] for r in rows}


def active_with_access_all(source_id: int, destination_ids: list[int]) -> set[int]:
    """Active accounts proven to reach the source and *every* destination.

    A multi-destination job may route any message to any of its destinations,
    so an account must have access to all of them to participate.
    """
    eligible: Optional[set[int]] = None
    for dest_id in destination_ids:
        s = active_with_access(source_id, dest_id)
        eligible = s if eligible is None else eligible & s
    return eligible or set()


def pending_active_checks(channel_kind: str, channel_id: int) -> int:
    """How many active accounts still have to probe this channel."""
    conn = db.get_connection()
    row = conn.execute(
        """SELECT COUNT(*) AS cnt FROM userbots u
            WHERE u.status = 'active'
              AND NOT EXISTS (SELECT 1 FROM channel_access ca
                               WHERE ca.channel_kind = ?
                                 AND ca.channel_id = ?
                                 AND ca.userbot_id = u.id)""",
        (channel_kind, channel_id),
    ).fetchone()
    return row["cnt"] if row else 0


def clear_for_channel(channel_kind: str, channel_id: int) -> None:
    """Drop every account's result so the channel is probed again from scratch."""
    conn = db.get_connection()
    conn.execute(
        "DELETE FROM channel_access WHERE channel_kind = ? AND channel_id = ?",
        (channel_kind, channel_id),
    )
    conn.commit()


def clear_for_userbot(userbot_id: int) -> None:
    """Drop one account's results (used when the account is removed)."""
    conn = db.get_connection()
    conn.execute("DELETE FROM channel_access WHERE userbot_id = ?", (userbot_id,))
    conn.commit()
