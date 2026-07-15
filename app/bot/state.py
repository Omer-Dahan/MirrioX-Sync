"""Per-user and global state storage — replaces PTB context.user_data / context.bot_data."""
from __future__ import annotations

# Per-user state dict keyed by Telegram user ID.
# Replaces python-telegram-bot's context.user_data.
_user_data: dict[int, dict] = {}

# Global bot state — replaces context.bot_data.
_bot_data: dict = {
    "admin_ids": [],       # set at startup
    "on_main_screen": False,
}


def get_user_data(uid: int) -> dict:
    """Return (and lazily create) the per-user state dict for the given user ID."""
    if uid not in _user_data:
        _user_data[uid] = {}
    return _user_data[uid]


def clear_user_data(uid: int) -> None:
    """Clear all state for a user (called on /start)."""
    _user_data.pop(uid, None)
