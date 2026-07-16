"""Hyper backup: per-account enable/disable, backup channel, and smart filters."""
from __future__ import annotations

import logging

from telethon import TelegramClient

from app.repositories import hyper_repo
from app.ui import renderer, texts, keyboards
from app.ui.keyboards import to_telethon
from app.bot import state as _state
from app.bot.handlers._common import update_main_message, answer_callback, delete_user_message

logger = logging.getLogger(__name__)


async def dispatch(bot: TelegramClient, event, uid: int) -> None:
    """Route hyper-related callback queries (data = 'hyp:<acc_id>:<action>[:...]')."""
    await answer_callback(event)
    data = event.data.decode()

    # Top-level management screen: the list of accounts (reached from Settings).
    if data == "hyp:list":
        text, kb = renderer.render_hyper_account_list()
        await update_main_message(bot, text, to_telethon(kb))
        return

    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "hyp":
        return
    try:
        acc_id = int(parts[1])
    except ValueError:
        return
    action = parts[2]

    if action == "menu":
        text, kb = renderer.render_hyper_menu(acc_id)

    elif action == "toggle":
        cfg = hyper_repo.get_config(acc_id)
        hyper_repo.set_enabled(acc_id, not bool(cfg and cfg["enabled"]))
        logger.info("Hyper: account #%d toggled -> %s", acc_id, not bool(cfg and cfg["enabled"]))
        text, kb = renderer.render_hyper_menu(acc_id)

    elif action == "pickdst":
        text, kb = renderer.render_hyper_dst_picker(acc_id)

    elif action == "dst" and len(parts) >= 4:
        try:
            dest_id = int(parts[3])
        except ValueError:
            return
        hyper_repo.set_destination(acc_id, dest_id)
        text, kb = renderer.render_hyper_menu(acc_id)

    elif action == "type" and len(parts) >= 4:
        text, kb = renderer.render_hyper_type(acc_id, parts[3])

    elif action == "ttog" and len(parts) >= 4:
        hyper_repo.toggle_type_enabled(acc_id, parts[3])
        text, kb = renderer.render_hyper_type(acc_id, parts[3])

    elif action == "comb" and len(parts) >= 4:
        mtype = parts[3]
        rule = hyper_repo.get_filter(acc_id, mtype)
        current = (rule or {}).get("combine", "and")
        hyper_repo.set_combine(acc_id, mtype, "or" if current == "and" else "and")
        text, kb = renderer.render_hyper_type(acc_id, mtype)

    elif action == "clr" and len(parts) >= 5:
        mtype, field = parts[3], parts[4]
        col = texts.HYPER_FIELD_COLUMNS.get(field)
        if col:
            hyper_repo.set_bound(acc_id, mtype, col, None)
        text, kb = renderer.render_hyper_type(acc_id, mtype)

    elif action == "set" and len(parts) >= 5:
        await _prompt_value(bot, uid, acc_id, parts[3], parts[4])
        return

    else:
        return

    await update_main_message(bot, text, to_telethon(kb))


async def _prompt_value(bot: TelegramClient, uid: int, acc_id: int, mtype: str, field: str) -> None:
    if mtype not in texts.HYPER_TYPES or field not in texts.HYPER_FIELD_COLUMNS:
        return
    ud = _state.get_user_data(uid)
    ud["awaiting_input"] = "hyper_value"
    ud["hyper_edit"] = {"acc_id": acc_id, "mtype": mtype, "field": field}
    await update_main_message(
        bot,
        texts.hyper_prompt_value(mtype, field),
        to_telethon(keyboards.kb_hyper_value_cancel(acc_id, mtype)),
    )


async def handle_hyper_value(bot: TelegramClient, event, uid: int) -> None:
    """Text-input step: a size (MB) or duration (minutes) bound. 0 clears it."""
    await delete_user_message(event)
    ud = _state.get_user_data(uid)
    edit = ud.get("hyper_edit")
    if not edit:
        ud.pop("awaiting_input", None)
        return
    acc_id, mtype, field = edit["acc_id"], edit["mtype"], edit["field"]

    raw = (event.message.text or "").strip().replace(",", ".")
    try:
        num = float(raw)
    except ValueError:
        # Keep awaiting the value so the user can retry.
        await update_main_message(
            bot,
            f"{texts.hyper_prompt_value(mtype, field)}\n\n⚠️ יש להזין מספר תקין.",
            to_telethon(keyboards.kb_hyper_value_cancel(acc_id, mtype)),
        )
        return

    col = texts.HYPER_FIELD_COLUMNS[field]
    if num <= 0:
        value = None  # clear the bound
    elif field in ("mindur", "maxdur"):
        value = int(round(num * 60))          # minutes → seconds
    else:
        value = int(round(num * 1024 * 1024))  # MB → bytes
    hyper_repo.set_bound(acc_id, mtype, col, value)

    ud.pop("awaiting_input", None)
    ud.pop("hyper_edit", None)
    text, kb = renderer.render_hyper_type(acc_id, mtype)
    await update_main_message(bot, text, to_telethon(kb))
