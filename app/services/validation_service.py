"""Input validation helpers. Return cleaned values or raise ValidationError."""
from __future__ import annotations

import re
from datetime import datetime
from app.models import ValidationError

_DATE_FORMATS = ["%d/%m/%Y %H:%M", "%d/%m/%Y"]


def validate_channel_ref(ref: str) -> str:
    """
    Normalise a channel reference.
    Accepts: @username, username, -100xxxxxxxxxx (numeric channel id), plain number.
    Returns the normalised ref string.
    """
    ref = ref.strip()
    if not ref:
        raise ValidationError("הפניה לערוץ לא יכולה להיות ריקה")

    # Private invite link: t.me/+hash or t.me/joinchat/hash — keep as-is
    if "t.me/+" in ref or "t.me/joinchat/" in ref:
        # Normalise to t.me/+hash format for storage
        if "t.me/joinchat/" in ref:
            hash_part = ref.split("t.me/joinchat/")[-1].strip()
            return f"t.me/+{hash_part}"
        return ref.strip()

    # If it's a URL like t.me/something, extract the username
    if "t.me/" in ref:
        parts = ref.split("t.me/")
        ref = "@" + parts[-1].strip().lstrip("@")

    # Pure negative integer (channel id like -1001234567890)
    if re.match(r"^-\d+$", ref):
        return ref

    # Plain positive integer — could be bare channel id or 100-prefixed channel id
    if re.match(r"^\d+$", ref):
        # If prefixed with 100 and long enough (>12 digits), it's a Bot API channel id
        if ref.startswith("100") and len(ref) > 12:
            return f"-{ref}"
        # Otherwise store as-is; worker resolves via PeerChannel
        return ref

    # @username or plain username
    username = ref.lstrip("@")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]{3,}$", username):
        raise ValidationError(
            "שם משתמש לא תקין. השתמש ב-@username, מזהה מספרי, או קישור t.me/"
        )
    return "@" + username


def validate_date_range(from_s: str, to_s: str) -> tuple[datetime, datetime]:
    from_s = from_s.strip()
    to_s = to_s.strip()

    date_from = parse_date(from_s, "תאריך התחלה")
    date_to = parse_date(to_s, "תאריך סיום")

    if date_from >= date_to:
        raise ValidationError("תאריך ההתחלה חייב להיות לפני תאריך הסיום")

    return date_from, date_to


def parse_date(value: str, label: str) -> datetime:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValidationError(
        f"פורמט {label} לא תקין. השתמש ב-DD/MM/YYYY או DD/MM/YYYY HH:MM"
    )


def validate_id_range(from_s: str, to_s: str) -> tuple[int, int]:
    id_from = _parse_positive_int(from_s, "מזהה התחלה")
    id_to = _parse_positive_int(to_s, "מזהה סיום")
    if id_from >= id_to:
        raise ValidationError("מזהה ההתחלה חייב להיות קטן ממזהה הסיום")
    return id_from, id_to


def validate_single_id(id_s: str) -> int:
    return _parse_positive_int(id_s, "מזהה הודעה")


def _parse_positive_int(value: str, label: str) -> int:
    value = value.strip()
    try:
        n = int(value)
    except ValueError as exc:
        raise ValidationError(f"{label} חייב להיות מספר שלם") from exc
    if n <= 0:
        raise ValidationError(f"{label} חייב להיות מספר חיובי")
    return n


def validate_word(word: str) -> str:
    word = word.strip()
    if not word:
        raise ValidationError("המילה לא יכולה להיות ריקה")
    if len(word) > 100:
        raise ValidationError("המילה ארוכה מדי (מקסימום 100 תווים)")
    return word


def validate_job_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValidationError("שם המשימה לא יכול להיות ריק")
    if len(name) > 80:
        raise ValidationError("שם המשימה ארוך מדי (מקסימום 80 תווים)")
    return name


def validate_telegram_id(id_s: str) -> int:
    id_s = id_s.strip()
    try:
        tid = int(id_s)
    except ValueError as exc:
        raise ValidationError("מזהה Telegram חייב להיות מספר שלם") from exc
    if tid <= 0:
        raise ValidationError("מזהה Telegram חייב להיות מספר חיובי")
    return tid


def validate_channel_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValidationError("שם הכינוי לא יכול להיות ריק")
    if len(name) > 60:
        raise ValidationError("שם הכינוי ארוך מדי (מקסימום 60 תווים)")
    return name


def validate_positive_int_setting(value: str, label: str, min_val: int, max_val: int) -> int:
    try:
        n = int(value.strip())
    except ValueError as exc:
        raise ValidationError(f"{label} חייב להיות מספר שלם") from exc
    if n < min_val or n > max_val:
        raise ValidationError(f"{label} חייב להיות בין {min_val} ל-{max_val}")
    return n
