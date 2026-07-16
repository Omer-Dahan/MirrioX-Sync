"""
Hyper backup filter engine.

Two concerns kept apart on purpose:
  - `hyper_media_type` / `extract_size_duration` read a Telethon message.
  - `evaluate` is a pure function over plain numbers, so the smart-filter logic
    (size / duration bounds, AND / OR) is unit-testable without Telegram.

Hyper captures media only: text and anything unclassifiable return media_type
None and are never backed up.
"""
from __future__ import annotations

from typing import Optional

# The media types a hyper rule can target. Audio (voice / music) is split out
# from 'file' so a duration bound on it is meaningful.
MEDIA_TYPES = ("video", "image", "file", "audio")


def hyper_media_type(msg) -> Optional[str]:
    """Classify a message into one of MEDIA_TYPES, or None for text/other/service."""
    media = getattr(msg, "media", None)
    if media is None:
        return None
    cls = media.__class__.__name__
    if cls == "MessageMediaUnsupported":
        return None
    if cls == "MessageMediaPhoto":
        return "image"
    if cls == "MessageMediaDocument":
        doc = getattr(media, "document", None)
        if doc is None:
            return None
        is_video = is_audio = is_sticker = is_animated = False
        for attr in getattr(doc, "attributes", None) or []:
            name = attr.__class__.__name__
            if name == "DocumentAttributeSticker":
                is_sticker = True
            elif name == "DocumentAttributeVideo":
                is_video = True
            elif name == "DocumentAttributeAnimated":
                is_animated = True
            elif name == "DocumentAttributeAudio":
                is_audio = True
        if is_sticker:
            return "image"
        if is_video or is_animated:
            return "video"
        if is_audio:
            return "audio"
        return "file"
    return None


def extract_size_duration(msg) -> tuple[Optional[int], Optional[int]]:
    """Return (size_bytes, duration_seconds) for a message; either may be None."""
    media = getattr(msg, "media", None)
    if media is None:
        return None, None
    cls = media.__class__.__name__
    if cls == "MessageMediaPhoto":
        photo = getattr(media, "photo", None)
        size: Optional[int] = None
        if photo is not None:
            for s in getattr(photo, "sizes", None) or []:
                sz = getattr(s, "size", None)
                if sz and (size is None or sz > size):
                    size = sz
        return size, None
    if cls == "MessageMediaDocument":
        doc = getattr(media, "document", None)
        if doc is None:
            return None, None
        size = getattr(doc, "size", None)
        duration: Optional[int] = None
        for attr in getattr(doc, "attributes", None) or []:
            d = getattr(attr, "duration", None)
            if d is not None:
                duration = int(d)  # video or audio track length, in seconds
        return size, duration
    return None, None


def evaluate(
    media_type: Optional[str],
    size: Optional[int],
    duration: Optional[int],
    rules: dict,
) -> tuple[bool, str]:
    """
    Decide whether a media message passes the hyper filter. Pure function.

    `rules` maps a media_type to a rule dict:
        {enabled, min_size, max_size, min_duration, max_duration, combine}
    A NULL/None bound is ignored. `combine` is 'and' (every set bound must hold)
    or 'or' (any set bound is enough). With no bounds set, everything passes.

    Returns (passes, reason) — reason is for logging/diagnostics only.
    """
    if media_type is None:
        return False, "not_media"
    rule = (rules or {}).get(media_type)
    if rule is None:
        return True, "no_rule"  # an unconfigured type is captured by default
    if not rule.get("enabled", True):
        return False, "type_disabled"

    conditions: list[bool] = []
    min_size = rule.get("min_size")
    max_size = rule.get("max_size")
    min_dur = rule.get("min_duration")
    max_dur = rule.get("max_duration")

    # An unknown value with a bound set fails that bound (conservative): we would
    # rather skip an item we cannot measure than let it past a filter meant to
    # keep it out.
    if min_size is not None:
        conditions.append(size is not None and size >= min_size)
    if max_size is not None:
        conditions.append(size is not None and size <= max_size)
    if min_dur is not None:
        conditions.append(duration is not None and duration >= min_dur)
    if max_dur is not None:
        conditions.append(duration is not None and duration <= max_dur)

    if not conditions:
        return True, "no_bounds"

    combine = (rule.get("combine") or "and").lower()
    if combine == "or":
        return (any(conditions), "combine_or")
    return (all(conditions), "combine_and")
