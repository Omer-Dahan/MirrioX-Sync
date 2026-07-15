"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    BOT_TOKEN: str
    TELETHON_API_ID: int
    TELETHON_API_HASH: str
    TELETHON_SESSION: str
    ADMIN_IDS: list[int]
    DB_PATH: str
    WORKER_POLL_INTERVAL_S: int


def load_config() -> Config:
    """Load and validate configuration from environment variables."""
    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN is required")

    api_id_raw = os.environ.get("TELETHON_API_ID", "").strip()
    if not api_id_raw:
        raise ValueError("TELETHON_API_ID is required")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise ValueError("TELETHON_API_ID must be an integer") from exc

    api_hash = os.environ.get("TELETHON_API_HASH", "").strip()
    if not api_hash:
        raise ValueError("TELETHON_API_HASH is required")

    session = os.environ.get("TELETHON_SESSION", "sessions/userbot").strip()

    admin_ids_raw = os.environ.get("ADMIN_IDS", "").strip()
    if not admin_ids_raw:
        raise ValueError("ADMIN_IDS must contain at least one Telegram user ID")
    try:
        admin_ids = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip()]
    except ValueError as exc:
        raise ValueError("ADMIN_IDS must be comma-separated integers") from exc
    if not admin_ids:
        raise ValueError("ADMIN_IDS must contain at least one Telegram user ID")

    db_path = os.environ.get("DB_PATH", "mirriox.db").strip()

    poll_interval_raw = os.environ.get("WORKER_POLL_INTERVAL_S", "5").strip()
    try:
        poll_interval = int(poll_interval_raw)
    except ValueError:
        poll_interval = 5

    return Config(
        BOT_TOKEN=bot_token,
        TELETHON_API_ID=api_id,
        TELETHON_API_HASH=api_hash,
        TELETHON_SESSION=session,
        ADMIN_IDS=admin_ids,
        DB_PATH=db_path,
        WORKER_POLL_INTERVAL_S=poll_interval,
    )
