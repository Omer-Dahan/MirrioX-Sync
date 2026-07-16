"""
Unit tests for the pure hyper filter engine (no Telegram / no DB needed).

Run manually:  python -m pytest tests/test_hyper_filter.py -q
"""
from __future__ import annotations

from app.services import hyper_filter


MB = 1024 * 1024


def _named(name: str, **attrs):
    """Build a throwaway object whose class name drives the classifier."""
    return type(name, (), attrs)()


def _video_msg(size_bytes: int, duration_s: int):
    attr = _named("DocumentAttributeVideo", duration=duration_s, round_message=False)
    doc = _named("Document", size=size_bytes, attributes=[attr])
    media = _named("MessageMediaDocument", document=doc)
    return _named("Message", media=media)


def _photo_msg(size_bytes: int):
    photo = _named("Photo", sizes=[_named("PhotoSize", size=size_bytes)])
    media = _named("MessageMediaPhoto", photo=photo)
    return _named("Message", media=media)


def _text_msg():
    return _named("Message", media=None)


# ── Classification ──────────────────────────────────────────────────────────────

def test_classifies_video_and_photo_and_text():
    assert hyper_filter.hyper_media_type(_video_msg(10 * MB, 60)) == "video"
    assert hyper_filter.hyper_media_type(_photo_msg(2 * MB)) == "image"
    assert hyper_filter.hyper_media_type(_text_msg()) is None


def test_extract_size_duration():
    size, dur = hyper_filter.extract_size_duration(_video_msg(350 * MB, 1300))
    assert size == 350 * MB
    assert dur == 1300
    size, dur = hyper_filter.extract_size_duration(_photo_msg(2 * MB))
    assert size == 2 * MB and dur is None


# ── The user's headline example: video ≥ 300MB AND ≥ 20min ──────────────────────

_VIDEO_RULE = {
    "enabled": True,
    "min_size": 300 * MB,
    "max_size": None,
    "min_duration": 20 * 60,
    "max_duration": None,
    "combine": "and",
}
RULES = {"video": _VIDEO_RULE}


def test_big_long_video_passes():
    ok, _ = hyper_filter.evaluate("video", 350 * MB, 25 * 60, RULES)
    assert ok is True


def test_big_but_short_video_fails_under_and():
    ok, _ = hyper_filter.evaluate("video", 350 * MB, 5 * 60, RULES)
    assert ok is False


def test_long_but_small_video_fails_under_and():
    ok, _ = hyper_filter.evaluate("video", 50 * MB, 25 * 60, RULES)
    assert ok is False


# ── OR semantics ────────────────────────────────────────────────────────────────

def test_or_passes_when_any_condition_holds():
    rule = dict(_VIDEO_RULE, combine="or")
    # small but long → passes because duration alone satisfies OR
    ok, _ = hyper_filter.evaluate("video", 50 * MB, 25 * 60, {"video": rule})
    assert ok is True
    # small and short → fails: neither condition holds
    ok, _ = hyper_filter.evaluate("video", 50 * MB, 5 * 60, {"video": rule})
    assert ok is False


# ── Defaults and edge cases ─────────────────────────────────────────────────────

def test_no_bounds_captures_everything():
    rule = {"enabled": True, "min_size": None, "max_size": None,
            "min_duration": None, "max_duration": None, "combine": "and"}
    ok, reason = hyper_filter.evaluate("file", 1, None, {"file": rule})
    assert ok is True and reason == "no_bounds"


def test_disabled_type_is_skipped():
    rule = {"enabled": False, "min_size": None, "max_size": None,
            "min_duration": None, "max_duration": None, "combine": "and"}
    ok, reason = hyper_filter.evaluate("image", 5 * MB, None, {"image": rule})
    assert ok is False and reason == "type_disabled"


def test_unconfigured_type_passes():
    ok, reason = hyper_filter.evaluate("image", 5 * MB, None, {})
    assert ok is True and reason == "no_rule"


def test_non_media_never_passes():
    ok, reason = hyper_filter.evaluate(None, None, None, RULES)
    assert ok is False and reason == "not_media"


def test_unknown_duration_fails_a_duration_bound():
    # A video whose duration metadata is missing must not slip past a min-duration filter.
    ok, _ = hyper_filter.evaluate("video", 350 * MB, None, RULES)
    assert ok is False
