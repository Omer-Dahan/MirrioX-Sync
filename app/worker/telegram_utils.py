"""Telethon entity-resolution helpers shared between worker modules."""
from __future__ import annotations

import logging
import re

from telethon import TelegramClient
from telethon.tl.types import PeerChannel, PeerChat

logger = logging.getLogger(__name__)


async def get_entity_safe(client: TelegramClient, ref: str):
    """
    Resolve a channel reference to a Telethon entity.
    Handles: @username, plain numeric ID, -100XXXXXXXXX (Bot API format),
             t.me/+hash (private invite links — joins the channel automatically).
    """
    ref = ref.strip()

    # Private invite link: t.me/+hash
    if ref.startswith("t.me/+") or ref.startswith("https://t.me/+"):
        hash_part = ref.split("t.me/+")[-1].strip()
        return await _join_or_get_via_invite(client, hash_part)

    # Bot API format: -1001234567890 → PeerChannel(1234567890)
    if re.match(r"^-100\d+$", ref):
        channel_id = int(ref[4:])  # strip "-100"
        return await client.get_entity(PeerChannel(channel_id))

    # Negative group ID: -1234567890 → PeerChat
    if re.match(r"^-\d+$", ref):
        chat_id = int(ref[1:])
        return await client.get_entity(PeerChat(chat_id))

    # Plain positive integer — treat as channel ID (PeerChannel)
    if re.match(r"^\d+$", ref):
        try:
            return await client.get_entity(PeerChannel(int(ref)))
        except Exception:  # pylint: disable=broad-exception-caught
            return await client.get_entity(int(ref))

    # @username or username
    return await client.get_entity(ref)


async def _join_or_get_via_invite(client: TelegramClient, invite_hash: str):
    """
    Join a private channel via invite link hash and return the entity.
    If already a member, returns the existing entity without re-joining.
    """
    from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
    from telethon.errors import UserAlreadyParticipantError, InviteHashExpiredError

    try:
        # Check the invite first to get chat info
        invite_info = await client(CheckChatInviteRequest(hash=invite_hash))
        chat = getattr(invite_info, "chat", None)
        if chat is not None:
            # Already a member — just return the entity
            return await client.get_entity(chat.id if hasattr(chat, "id") else chat)
    except Exception:  # nosec B110 — non-fatal; proceed to join attempt
        pass

    try:
        result = await client(ImportChatInviteRequest(hash=invite_hash))
        chats = getattr(result, "chats", [])
        if chats:
            return await client.get_entity(chats[0].id)
        return await client.get_entity(result)
    except UserAlreadyParticipantError:
        # Already a member — the CheckChatInviteRequest above should have returned chat,
        # but as a fallback try resolving via the hash directly
        logger.info("Already a participant for invite hash %s — resolving entity", invite_hash)
        raise ValueError(
            f"Already a member of this channel (invite hash: {invite_hash}). "
            "Please provide the channel @username or numeric ID instead."
        )
    except InviteHashExpiredError:
        raise ValueError(f"Invite link has expired or is invalid (hash: {invite_hash})")
