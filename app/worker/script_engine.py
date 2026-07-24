"""Executes an ad-hoc Python snippet against a userbot's live Telethon client.

This is a deliberate, admin-only remote-execution feature: the code comes from an
authorised admin over the control bot and runs on that admin's own account. It is
gated by ADMIN_IDS at the bot layer and by the `adhoc_enabled` setting.

The snippet body is wrapped in an async function so it may use `await`. A
task-local `print` and the return value are captured and returned as the run's
output; any exception is returned as a formatted traceback. Delivery of live
messages/files back to the admin is done through helpers injected into the
snippet's namespace.
"""
from __future__ import annotations

import asyncio
import csv as _csv
import datetime as _datetime
import io
import json as _json
import logging
import textwrap
import traceback

logger = logging.getLogger(__name__)

# Wall-clock ceiling before a snippet is cancelled. The run happens inside the
# account's poll loop, so an unbounded snippet would stall the rest of that
# account's work.
#
# NOTE: this is a soft limit, not a hard kill. asyncio.wait_for can only *cancel*
# the task, which does nothing against a tight CPU loop ("while True: pass") and
# can be swallowed by a snippet that catches CancelledError. A truly enforceable
# limit needs process isolation (a subprocess with a real kill), which in turn
# needs an RPC bridge to the live Telethon client the snippet runs against — a
# larger change tracked separately. Treat this as a guardrail, not a sandbox.
ADHOC_TIMEOUT_S = 300

# How much captured output we keep; the notification truncates further.
_MAX_OUTPUT_CHARS = 8000


async def _deliver_text(chat_id, text: str) -> None:
    if chat_id is None:
        return
    from app.bot import bot_main
    await bot_main.send_notification(int(chat_id), text)


async def _deliver_file(chat_id, file, caption=None, filename=None) -> None:
    if chat_id is None:
        return
    from app.bot import bot_main
    await bot_main.send_document(int(chat_id), file, caption=caption, filename=filename)


async def run_snippet(client, code: str, chat_id=None) -> tuple[str, str]:
    """
    Run one async snippet against `client`. Returns (status, output_text).

    status is 'done' on success or 'error' on any exception/timeout.

    Injected names available to the snippet:
      client                                  -- the account's Telethon client
      me                                      -- await client.get_me() (best effort)
      send(text)                              -- send a text message to the admin
      send_file(file, caption=, filename=)    -- send a document to the admin
                                                 (file: path str or bytes)
      print(...)                              -- captured into this run's output
      asyncio, datetime, json, csv, io        -- commonly useful modules
    The snippet may also `import` anything else it needs.
    """
    me = None
    try:
        me = await client.get_me()
    except Exception:  # nosec B110 — 'me' is a convenience, not required
        pass

    async def _send(text) -> None:
        await _deliver_text(chat_id, str(text))

    async def _send_file(file, caption=None, filename=None) -> None:
        await _deliver_file(chat_id, file, caption=caption, filename=filename)

    # Task-local output. Injected as `print`, so a snippet's output lands here and
    # never on the process stdout — two concurrent runs on different accounts can't
    # bleed into each other (which a global redirect_stdout would allow). Only the
    # `os` module is deliberately dropped from the namespace; note this is a
    # convenience trim, not a sandbox — the snippet can still `import os`.
    buf = io.StringIO()

    def _print(*args, sep=" ", end="\n", **_kwargs) -> None:
        buf.write(sep.join(str(a) for a in args) + end)

    namespace = {
        "client": client,
        "me": me,
        "send": _send,
        "send_file": _send_file,
        "print": _print,
        "asyncio": asyncio,
        "datetime": _datetime,
        "json": _json,
        "csv": _csv,
        "io": io,
    }

    wrapper = "async def __snippet__():\n" + textwrap.indent(code, "    ")
    try:
        compiled = compile(wrapper, "<adhoc-snippet>", "exec")
    except SyntaxError:
        return "error", f"SyntaxError:\n{traceback.format_exc()}"[:_MAX_OUTPUT_CHARS]

    exec(compiled, namespace)  # nosec B102 — admin-only, deliberate RCE feature
    snippet = namespace["__snippet__"]

    try:
        result = await asyncio.wait_for(snippet(), timeout=ADHOC_TIMEOUT_S)
    except asyncio.TimeoutError:
        out = buf.getvalue()
        return "error", (out + f"\n⏱ TimeoutError: exceeded {ADHOC_TIMEOUT_S}s").strip()[:_MAX_OUTPUT_CHARS]
    except Exception:
        out = buf.getvalue()
        return "error", (out + "\n" + traceback.format_exc()).strip()[:_MAX_OUTPUT_CHARS]

    out = buf.getvalue()
    if result is not None:
        tail = f"<return> {result!r}"
        out = f"{out}\n{tail}".strip() if out else tail
    return "done", (out or "(no output)")[:_MAX_OUTPUT_CHARS]
