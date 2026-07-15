"""
Create anonymous Telegraph reports for completed jobs.

Reports contain t.me links to failed / unexpectedly-skipped messages only.
No job name, no channel name — neutral content per user requirement.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_API = "https://api.telegra.ph"
_MAX_ITEMS = 500  # cap to stay well within Telegraph's size limit


# ── Low-level HTTP ─────────────────────────────────────────────────────────────

def _post_sync(method: str, params: dict) -> dict:
    url = f"{_API}/{method}"
    data = json.dumps(params, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310 — URL is hardcoded to trusted Telegraph API
        return json.loads(resp.read().decode("utf-8"))


# ── Account token ──────────────────────────────────────────────────────────────

def _get_or_create_token_sync() -> str:
    from app.repositories import state_repo
    token = state_repo.get_setting("telegraph_token")
    if token:
        return token
    resp = _post_sync("createAccount", {
        "short_name": "msg_report",
        "author_name": "Message Report",
    })
    if not resp.get("ok"):
        raise RuntimeError(f"Telegraph createAccount failed: {resp}")
    token = resp["result"]["access_token"]
    state_repo.set_setting("telegraph_token", token)
    return token


# ── Link builder ───────────────────────────────────────────────────────────────

def _msg_link(msg_id: int, resolved_id: Optional[int], channel_ref: str) -> str:
    """Build a t.me deep link to a specific message."""
    if resolved_id:
        # resolved_id is already the bare channel ID (Telethon entity.id)
        return f"https://t.me/c/{resolved_id}/{msg_id}"
    # Fall back to username / ref
    ref = channel_ref.lstrip("@")
    if ref.startswith("t.me/"):
        ref = ref[5:]
    ref = ref.split("/")[-1]
    return f"https://t.me/{ref}/{msg_id}"


# ── Content builder ────────────────────────────────────────────────────────────

def _build_content(
    messages: list[dict],
    resolved_id: Optional[int],
    channel_ref: str,
) -> list:
    nodes: list = []

    failed  = [m for m in messages if m["status"] == "failed"][:_MAX_ITEMS]
    skipped = [m for m in messages if m["status"] == "skipped"][:_MAX_ITEMS]

    if failed:
        nodes.append({"tag": "h4", "children": [f"Failed ({len(failed)})"]})
        for m in failed:
            link = _msg_link(m["msg_id"], resolved_id, channel_ref)
            reason = m.get("reason") or "error"
            nodes.append({"tag": "p", "children": [
                {"tag": "a", "attrs": {"href": link}, "children": [f"#{m['msg_id']}"]},
                f"  —  {reason}",
            ]})

    if skipped:
        nodes.append({"tag": "h4", "children": [f"Skipped ({len(skipped)})"]})
        for m in skipped:
            link = _msg_link(m["msg_id"], resolved_id, channel_ref)
            reason = m.get("reason") or "skipped"
            nodes.append({"tag": "p", "children": [
                {"tag": "a", "attrs": {"href": link}, "children": [f"#{m['msg_id']}"]},
                f"  —  {reason}",
            ]})

    return nodes


# ── Public API ─────────────────────────────────────────────────────────────────

def create_sync(
    job_id: int,
    messages: list[dict],
    resolved_id: Optional[int],
    channel_ref: str,
) -> Optional[str]:
    """Synchronous Telegraph page creation. Returns URL or None."""
    if not messages:
        return None
    try:
        token = _get_or_create_token_sync()
        content = _build_content(messages, resolved_id, channel_ref)
        resp = _post_sync("createPage", {
            "access_token": token,
            "title": f"Report #{job_id}",
            "content": content,
            "return_content": False,
        })
        if resp.get("ok"):
            return resp["result"]["url"]
        logger.warning("Telegraph createPage failed: %s", resp)
        return None
    except Exception as exc:
        logger.warning("Telegraph report failed for job #%d: %s", job_id, exc)
        return None


async def create_report(
    job_id: int,
    messages: list[dict],
    resolved_id: Optional[int],
    channel_ref: str,
) -> Optional[str]:
    """Async wrapper — runs the sync call in a thread so the event loop is not blocked."""
    return await asyncio.to_thread(create_sync, job_id, messages, resolved_id, channel_ref)


# ── Duplicates report ──────────────────────────────────────────────────────────

def _format_size(size_bytes: Optional[int]) -> str:
    if not size_bytes:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.0f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} GB"


def _build_duplicates_content(
    chunk: list[dict],
    get_items_fn,
    username: Optional[str],
    resolved_id: Optional[int],
    start_idx: int,
    total_len: int,
    next_url: Optional[str],
) -> list:
    """Build Telegraph content nodes for a chunk of duplicate groups."""
    nodes: list = []
    nodes.append({"tag": "p", "children": [f"סה\"כ קבוצות בערוץ: {total_len}"]})

    for idx, group in enumerate(chunk, start_idx):
        mime = group.get("mime_type") or "unknown"
        size_str = _format_size(group.get("file_size"))
        count = group["total_count"]
        label = f"{idx}. {mime}"
        if size_str:
            label += f" ({size_str})"
        label += f" — ×{count} כפילויות"
        nodes.append({"tag": "h4", "children": [label]})

        items = get_items_fn(group["media_id"])
        for i, item in enumerate(items):
            msg_id = item["message_id"]
            if resolved_id:
                link = f"https://t.me/c/{resolved_id}/{msg_id}"
            elif username:
                ref = username.lstrip("@")
                link = f"https://t.me/{ref}/{msg_id}"
            else:
                link = f"#{msg_id}"

            note = " (מקורי)" if i == 0 else " (כפול)"
            nodes.append({"tag": "p", "children": [
                {"tag": "a", "attrs": {"href": link}, "children": [f"#{msg_id}"]},
                note,
            ]})

    if next_url:
        nodes.append({"tag": "h4", "children": ["עוד כפילויות..."]})
        nodes.append({"tag": "p", "children": [
            {"tag": "a", "attrs": {"href": next_url}, "children": ["לעמוד הבא ⬅️"]}
        ]})

    return nodes


def create_duplicates_report_sync(
    scan_id: int,
    groups: list[dict],
    get_items_fn,
    username: Optional[str],
    resolved_id: Optional[int],
) -> Optional[str]:
    """Synchronous Telegraph page creation for duplicates report. Returns URL or None.
    Automatically paginates large duplicate reports into multiple Telegraph pages.
    """
    if not groups:
        return None
    try:
        token = _get_or_create_token_sync()
        chunk_size = 150
        chunks = [groups[i:i+chunk_size] for i in range(0, len(groups), chunk_size)]
        
        current_url = None
        for chunk_idx in reversed(range(len(chunks))):
            chunk = chunks[chunk_idx]
            start_idx = chunk_idx * chunk_size + 1
            
            content = _build_duplicates_content(
                chunk=chunk,
                get_items_fn=get_items_fn,
                username=username,
                resolved_id=resolved_id,
                start_idx=start_idx,
                total_len=len(groups),
                next_url=current_url,
            )
            title = f"Duplicates #{scan_id}" + (f" (Page {chunk_idx + 1})" if len(chunks) > 1 else "")
            
            resp = _post_sync("createPage", {
                "access_token": token,
                "title": title,
                "content": content,
                "return_content": False,
            })
            if resp.get("ok"):
                current_url = resp["result"]["url"]
            else:
                logger.warning("Telegraph createPage (duplicates) failed at chunk %d: %s", chunk_idx, resp)
                # If page 1 fails but page 2 succeeded, we just return page 2 URL.
                # Though ideally it won't fail.

        return current_url
    except Exception as exc:
        logger.warning("Telegraph duplicates report failed for scan #%d: %s", scan_id, exc)
        return None


async def create_duplicates_report(
    scan_id: int,
    groups: list[dict],
    get_items_fn,
    username: Optional[str],
    resolved_id: Optional[int],
) -> Optional[str]:
    """Async wrapper for duplicates report."""
    return await asyncio.to_thread(
        create_duplicates_report_sync,
        scan_id, groups, get_items_fn, username, resolved_id,
    )
