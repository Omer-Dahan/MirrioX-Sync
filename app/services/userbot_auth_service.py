"""
Interactive userbot sign-in, driven from the management bot UI.

Telethon's login is stateful: send_code_request → sign_in(code) → optionally
sign_in(password) for 2FA. The client must stay alive across those steps, so a
pending login is held in memory per admin user and torn down on success,
cancel, or timeout.

Sessions are written to their own file per account (sessions/userbot_<phone>),
so accounts never share credentials.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
    SessionPasswordNeededError,
)

from app.config import Config
from app.models import ValidationError
from app.repositories import userbot_repo

logger = logging.getLogger(__name__)

# A login left untouched this long is abandoned and cleaned up.
LOGIN_TIMEOUT_S = 15 * 60


@dataclass
class PendingLogin:
    """One admin's in-progress sign-in."""
    phone: str
    session_name: str
    client: TelegramClient
    phone_code_hash: Optional[str] = None
    needs_password: bool = False
    created_at: float = field(default_factory=time.monotonic)

    def is_stale(self) -> bool:
        return (time.monotonic() - self.created_at) > LOGIN_TIMEOUT_S


# admin uid → PendingLogin
_pending: dict[int, PendingLogin] = {}


def get_pending(uid: int) -> Optional[PendingLogin]:
    login = _pending.get(uid)
    if login is not None and login.is_stale():
        asyncio.ensure_future(cancel(uid))
        return None
    return login


async def cancel(uid: int) -> None:
    """Abort a pending login and clean up its half-built session file."""
    login = _pending.pop(uid, None)
    if login is None:
        return
    try:
        await login.client.disconnect()
    except Exception:  # nosec B110 — best-effort teardown
        pass
    # A session that never signed in is useless — don't leave the file behind.
    remove_session_files(login.session_name)


def remove_session_files(session_name: str) -> None:
    for suffix in (".session", ".session-journal"):
        path = f"{session_name}{suffix}"
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:  # nosec B110 — leftover file is harmless
            pass


def normalize_phone(raw: str) -> str:
    """Validate and normalise a phone number to +<digits>."""
    cleaned = "".join(ch for ch in (raw or "") if ch.isdigit() or ch == "+")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    digits = cleaned[1:]
    if not digits.isdigit() or not (7 <= len(digits) <= 15):
        raise ValidationError(
            "מספר טלפון לא תקין. הזן עם קידומת מדינה, לדוגמה: +972501234567"
        )
    return cleaned


async def start_login(uid: int, config: Config, raw_phone: str) -> None:
    """
    Step 1 — validate the phone, connect a fresh client and request a code.
    Raises ValidationError with a Hebrew message on any user-fixable problem.
    """
    phone = normalize_phone(raw_phone)

    existing = userbot_repo.get_by_phone(phone)
    if existing is not None:
        raise ValidationError(f"המספר {phone} כבר רשום כחשבון יוזרבוט.")

    await cancel(uid)  # drop any earlier attempt by this admin

    session_name = userbot_repo.build_session_name(phone)
    session_dir = os.path.dirname(session_name)
    if session_dir:
        os.makedirs(session_dir, exist_ok=True)
    # A removed account can leave its session file behind (Windows may still have
    # it locked at delete time). Start from a clean slate so we never resurrect
    # stale credentials under a reused name.
    remove_session_files(session_name)

    client = TelegramClient(session_name, config.TELETHON_API_ID, config.TELETHON_API_HASH)
    try:
        await client.connect()
    except Exception as e:
        remove_session_files(session_name)
        raise ValidationError(f"שגיאת חיבור לטלגרם: {e}") from e

    try:
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError as e:
        await _abort(client, session_name)
        raise ValidationError("מספר הטלפון אינו תקין בטלגרם.") from e
    except PhoneNumberBannedError as e:
        await _abort(client, session_name)
        raise ValidationError("מספר הטלפון חסום בטלגרם.") from e
    except ApiIdInvalidError as e:
        await _abort(client, session_name)
        raise ValidationError("TELETHON_API_ID / TELETHON_API_HASH אינם תקינים.") from e
    except FloodWaitError as e:
        await _abort(client, session_name)
        raise ValidationError(
            f"טלגרם מגביל בקשות זמנית. נסה שוב בעוד {e.seconds} שניות."
        ) from e
    except Exception as e:
        await _abort(client, session_name)
        raise ValidationError(f"שליחת הקוד נכשלה: {e}") from e

    _pending[uid] = PendingLogin(
        phone=phone,
        session_name=session_name,
        client=client,
        phone_code_hash=getattr(sent, "phone_code_hash", None),
    )
    logger.info("Userbot login: code requested for %s (admin %d)", phone, uid)


async def submit_code(uid: int, raw_code: str) -> bool:
    """
    Step 2 — submit the login code.
    Returns True when signed in, False when 2FA is still required.
    """
    login = get_pending(uid)
    if login is None:
        raise ValidationError("תהליך ההתחברות פג או בוטל. התחל מחדש.")

    code = "".join(ch for ch in (raw_code or "") if ch.isdigit())
    if not code:
        raise ValidationError("הזן את הקוד שקיבלת בטלגרם (ספרות בלבד).")

    try:
        await login.client.sign_in(
            phone=login.phone, code=code, phone_code_hash=login.phone_code_hash
        )
    except SessionPasswordNeededError:
        login.needs_password = True
        logger.info("Userbot login: 2FA required for %s", login.phone)
        return False
    except PhoneCodeInvalidError as e:
        raise ValidationError("הקוד שגוי. נסה שוב.") from e
    except PhoneCodeExpiredError as e:
        await cancel(uid)
        raise ValidationError("הקוד פג תוקף. התחל את התהליך מחדש.") from e
    except FloodWaitError as e:
        await cancel(uid)
        raise ValidationError(f"טלגרם מגביל בקשות. נסה שוב בעוד {e.seconds} שניות.") from e
    except Exception as e:
        raise ValidationError(f"ההתחברות נכשלה: {e}") from e

    await _finalize(uid, login)
    return True


async def submit_password(uid: int, password: str) -> None:
    """Step 3 — submit the 2FA password."""
    login = get_pending(uid)
    if login is None:
        raise ValidationError("תהליך ההתחברות פג או בוטל. התחל מחדש.")

    if not password:
        raise ValidationError("הזן את סיסמת ה-2FA.")

    try:
        await login.client.sign_in(password=password)
    except PasswordHashInvalidError as e:
        raise ValidationError("סיסמת 2FA שגויה. נסה שוב.") from e
    except FloodWaitError as e:
        await cancel(uid)
        raise ValidationError(f"טלגרם מגביל בקשות. נסה שוב בעוד {e.seconds} שניות.") from e
    except Exception as e:
        raise ValidationError(f"אימות 2FA נכשל: {e}") from e

    await _finalize(uid, login)


async def _finalize(uid: int, login: PendingLogin) -> None:
    """Persist the freshly authorised account and release the login client."""
    try:
        me = await login.client.get_me()
        telegram_id = getattr(me, "id", None)
        username = getattr(me, "username", None)
        name = getattr(me, "first_name", None) or username or login.phone

        # Same Telegram account added twice under different numbers.
        if telegram_id is not None:
            duplicate = userbot_repo.get_by_telegram_id(telegram_id)
            if duplicate is not None:
                await login.client.disconnect()
                _pending.pop(uid, None)
                remove_session_files(login.session_name)
                raise ValidationError(
                    f"החשבון הזה כבר רשום ({duplicate.display()})."
                )

        userbot_repo.create(
            name=name,
            phone=login.phone,
            session_name=login.session_name,
            telegram_id=telegram_id,
            username=username,
            status="active",
            is_default=False,
        )
        logger.info(
            "Userbot added: %s (%s, id=%s) session=%s",
            name, login.phone, telegram_id, login.session_name,
        )
    finally:
        # Release the session file so the worker's own client can open it.
        try:
            await login.client.disconnect()
        except Exception:  # nosec B110 — best-effort teardown
            pass
        _pending.pop(uid, None)

    # A new account may reach channels the others couldn't — let failed jobs retry.
    from app.repositories import job_repo
    reset = job_repo.reset_all_exclusions()
    if reset:
        logger.info("Cleared channel-access exclusions on %d job(s) after adding an account", reset)


async def _abort(client: TelegramClient, session_name: str) -> None:
    try:
        await client.disconnect()
    except Exception:  # nosec B110 — best-effort teardown
        pass
    remove_session_files(session_name)
