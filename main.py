"""
Mirriox — entry point.

Usage:
  python main.py          Run bot + worker together (default)
  python main.py bot      Run the management bot only
  python main.py worker   Run the userbot worker only
  python main.py setup    Authenticate the Telethon session interactively
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys

from app.network_errors import is_network_error

# One PID file per mode, so `bot` and `worker` can run side by side as separate
# processes. A single shared file made every start terminate every other mode.
_PID_FILE_TMPL = "mirriox.{mode}.pid"

# Which already-running modes a starting mode must terminate. Two processes
# conflict when they would share the SQLite writer role or the same Telethon
# session files — `bot` and `worker` share neither, so they are deliberately
# absent from each other's lists.
_CONFLICTING_MODES: dict[str, tuple[str, ...]] = {
    "all":    ("all", "bot", "worker"),
    "bot":    ("all", "bot"),
    "worker": ("all", "worker"),
    # setup signs in on the default session file, which the worker holds open.
    "setup":  ("all", "worker"),
}


def _pid_path(mode: str) -> str:
    return _PID_FILE_TMPL.format(mode=mode)


def _is_mirriox_process(pid: int) -> bool:
    """
    Best-effort check that `pid` really is a Mirriox process.

    Linux recycles PIDs aggressively, so a PID file left behind by a crash or a
    hard reboot can point at an unrelated process that must not be signalled.
    /proc is the only cheap way to ask; where it doesn't exist (Windows) there is
    nothing to verify against, so the PID file is taken at face value as before.
    """
    if not os.path.isdir("/proc"):
        return True
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
    except OSError:
        return False  # process vanished between the liveness check and now
    return "main.py" in cmdline


def _kill_old_process(mode: str) -> None:
    """Terminate previously running Mirriox processes that conflict with this mode."""
    for other in _CONFLICTING_MODES.get(mode, ()):
        _kill_recorded_process(_pid_path(other))


def _kill_recorded_process(pid_file: str) -> None:
    """Terminate the process recorded in one PID file, if it is still ours."""
    logger = logging.getLogger(__name__)
    if not os.path.exists(pid_file):
        return
    try:
        with open(pid_file) as f:
            old_pid = int(f.read().strip())
    except (ValueError, OSError):
        return

    if old_pid == os.getpid():
        return  # shouldn't happen, but guard against self-kill

    try:
        os.kill(old_pid, 0)  # check if process is alive
    except OSError:
        return  # already gone

    if not _is_mirriox_process(old_pid):
        logger.warning(
            "PID %d in %s belongs to another program — leaving it alone",
            old_pid, pid_file,
        )
        return

    try:
        if sys.platform == "win32":
            import subprocess
            subprocess.call(
                ["taskkill", "/F", "/PID", str(old_pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.kill(old_pid, signal.SIGTERM)
        logger.info("Terminated old process (PID %d, from %s)", old_pid, pid_file)
    except OSError as e:
        logger.warning("Could not terminate old process %d: %s", old_pid, e)


def _write_pid(mode: str) -> None:
    with open(_pid_path(mode), "w") as f:
        f.write(str(os.getpid()))


def _remove_pid(mode: str) -> None:
    try:
        os.remove(_pid_path(mode))
    except OSError:
        pass


class _CollapseNetworkErrors(logging.Filter):
    """
    Replace multi-line tracebacks for transient network errors with a single
    WARNING line. Keeps the log clean without hiding real problems.
    """
    _NETWORK_EXC_NAMES = frozenset({
        "NetworkError", "ConnectError", "ReadError",
        "RemoteProtocolError", "ConnectionError", "ConnectionResetError",
        "TimeoutError",
    })
    NOISY_LOGGERS = frozenset({
        "telegram.ext.Updater",
        "telegram.ext.Application",
        "asyncio",
    })

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if record.name not in self.NOISY_LOGGERS:
            return True
        if not record.exc_info:
            return True
        exc_type = record.exc_info[0]
        if exc_type is None:
            return True
        # Walk the MRO to catch subclasses (e.g. telegram.error.NetworkError)
        names = {cls.__name__ for cls in exc_type.__mro__}
        if names & self._NETWORK_EXC_NAMES:
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
            record.exc_info = None
            record.exc_text = None
            # Shorten the message to one line
            record.msg = "ניתוק רשת זמני (%s) — ממשיך לנסות..."
            record.args = (exc_type.__name__,)
        return True


class _TelethonReconnectFilter(logging.Filter):
    """
    Condenses Telethon's internal reconnect spam into exactly two lines:
      - ONE warning when the connection first drops.
      - ONE info  when the connection is successfully restored.
    All intermediate "Attempt N at connecting failed" lines are suppressed.
    A single shared instance should be attached to both noisy loggers so
    only one pair of messages is emitted per disconnect cycle.
    """

    def __init__(self) -> None:
        super().__init__()
        self._disconnected = False  # tracks if we already emitted the drop warning

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        msg = record.getMessage()

        # ── Disconnect signal ────────────────────────────────────────────────
        # Matches: "Server closed the connection" and "Attempt N at connecting failed"
        is_drop = "Server closed the connection" in msg or "connecting failed" in msg
        if is_drop:
            if not self._disconnected:
                # Emit ONE consolidated warning
                self._disconnected = True
                record.levelno  = logging.WARNING
                record.levelname = "WARNING"
                record.msg  = "⚠️ ניתוק רשת — מנסה להתחבר מחדש בשקט..."
                record.args = ()
                return True
            # Suppress all subsequent attempt lines
            return False

        # ── Reconnect success ────────────────────────────────────────────────
        # Telethon logs "Connection to <DC> restored" at INFO level on success
        if self._disconnected and "restored" in msg:
            self._disconnected = False
            record.levelno  = logging.INFO
            record.levelname = "INFO"
            record.msg  = "✅ חיבור לרשת חודש בהצלחה"
            record.args = ()
            return True

        return True


def _force_utf8_output() -> None:
    """
    Make stdout/stderr UTF-8 regardless of the system locale.

    Log messages contain Hebrew and emoji. Under a non-UTF-8 locale (a systemd
    unit with no LANG set defaults to POSIX/ASCII) writing them would raise
    UnicodeEncodeError from inside the logging handler.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _add_rotating_file_handler(formatter: logging.Formatter) -> None:
    """
    Mirror all logging to an hourly-rotating text file, keeping ~24 hours back.

    The service runs on a headless Linux box where stdout is not captured to a
    file, so a crash or a subtle bug (e.g. an account sending past its daily cap)
    leaves nothing to inspect afterwards. An hourly TimedRotatingFileHandler with
    backupCount = retention_hours keeps exactly that window on disk and deletes
    anything older automatically, so the logs never grow without bound.

    Controlled by environment variables (all optional):
      LOG_TO_FILE          "1"/"0" — enable file logging (default: enabled)
      LOG_DIR              directory for the log files (default: "logs")
      LOG_RETENTION_HOURS  how many hours back to keep (default: 24)
    """
    if os.environ.get("LOG_TO_FILE", "1").strip() not in ("1", "true", "True"):
        return

    log_dir = os.environ.get("LOG_DIR", "logs").strip() or "logs"
    try:
        retention_hours = int(os.environ.get("LOG_RETENTION_HOURS", "24").strip())
    except ValueError:
        retention_hours = 24
    retention_hours = max(retention_hours, 1)

    try:
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.TimedRotatingFileHandler(
            filename=os.path.join(log_dir, "mirriox.log"),
            when="H",            # rotate every hour on the hour
            interval=1,
            backupCount=retention_hours,  # keep this many rotated files → hours back
            encoding="utf-8",
            utc=True,            # match the UTC timestamps used elsewhere
        )
        handler.setFormatter(formatter)
        logging.getLogger().addHandler(handler)
    except OSError as exc:
        # A logging problem must never take the whole service down — stderr still works.
        logging.getLogger(__name__).warning("Could not enable file logging: %s", exc)


def _setup_logging() -> None:
    _force_utf8_output()
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=date_format,
    )
    _add_rotating_file_handler(logging.Formatter(log_format, datefmt=date_format))
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

    # One shared stateful filter: emits ONE warning on disconnect, ONE info on restore.
    # Both loggers share the same instance so only one pair of messages is emitted.
    _reconnect_filter = _TelethonReconnectFilter()
    logging.getLogger("telethon.network.connection.connection").setLevel(logging.WARNING)
    logging.getLogger("telethon.network.connection.connection").addFilter(_reconnect_filter)
    logging.getLogger("telethon.network.mtprotosender").setLevel(logging.INFO)
    logging.getLogger("telethon.network.mtprotosender").addFilter(_reconnect_filter)

    # Collapse noisy network-error tracebacks to single WARNING lines
    _f = _CollapseNetworkErrors()
    for name in _CollapseNetworkErrors.NOISY_LOGGERS:
        logging.getLogger(name).addFilter(_f)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mirriox Telegram Copier")
    parser.add_argument(
        "mode",
        nargs="?",
        default="all",
        choices=["all", "bot", "worker", "setup"],
        help="Component to run (default: all — bot + worker together)",
    )
    return parser.parse_args()


def main() -> None:
    _setup_logging()
    logger = logging.getLogger(__name__)

    # Parsed before anything else: which processes to terminate depends on the mode.
    args = _parse_args()
    _kill_old_process(args.mode)

    # setup is a short interactive command, not a long-running component — it
    # claims no PID file of its own and nothing needs to terminate it.
    if args.mode == "setup":
        _run_main(logger, args)
        return

    _write_pid(args.mode)
    try:
        _run_main(logger, args)
    finally:
        _remove_pid(args.mode)


def _run_main(logger: logging.Logger, args: argparse.Namespace) -> None:
    from app.config import load_config
    from app import db

    config = load_config()
    db.init(config.DB_PATH)
    db.init_schema()

    # Register the .env session as the default userbot account. Done here rather
    # than in the worker alone so the account is listed in the bot UI in every
    # mode — including `bot`, where no worker ever runs. Idempotent.
    from app.repositories import userbot_repo
    userbot_repo.ensure_default(config.TELETHON_SESSION)

    if args.mode == "all":
        logger.info("Starting bot + worker together (DB: %s)", config.DB_PATH)
        asyncio.run(_run_all(config))

    elif args.mode == "bot":
        logger.info("Starting management bot only (DB: %s)", config.DB_PATH)
        from app.bot.bot_main import run
        run(config)

    elif args.mode == "worker":
        logger.info("Starting userbot worker only (DB: %s)", config.DB_PATH)
        from app.worker.worker_main import run
        run(config)

    elif args.mode == "setup":
        _run_setup(config)



async def _run_with_restart(name: str, coro_fn, config) -> None:
    """Wrap a long-running coroutine with automatic restart on network errors.

    Always retries after a fixed 5s delay so recovery is quick after the
    network comes back (no exponential backoff that grows to minutes).
    """
    _logger = logging.getLogger(__name__)
    _RETRY_DELAY_S = 5
    while True:
        try:
            await coro_fn(config)
            return  # clean exit
        except Exception as exc:
            if is_network_error(exc):
                _logger.warning(
                    "[%s] ניתוק רשת (%s) — מנסה שוב בעוד %ds...",
                    name, exc, _RETRY_DELAY_S,
                )
                await asyncio.sleep(_RETRY_DELAY_S)
                _logger.info("[%s] 🔄 מנסה להתחבר מחדש...", name)
            else:
                _logger.exception("[%s] שגיאה קריטית — מפסיק: %s", name, exc)
                raise


def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """
    Suppress the known Windows asyncio bug where a socket is already invalid
    when asyncio tries to shut it down (WinError 10022).
    All other exceptions go through the default handler.
    """
    exc = context.get("exception")
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10022:
        return  # harmless Windows socket-cleanup race — ignore
    loop.default_exception_handler(context)


async def _run_all(config) -> None:
    """Run bot and worker concurrently, with auto-restart on network errors."""
    asyncio.get_running_loop().set_exception_handler(_asyncio_exception_handler)

    from app.bot.bot_main import run_async as bot_run_async
    from app.worker.worker_main import run_async as worker_run_async

    worker_task = asyncio.create_task(
        _run_with_restart("worker", worker_run_async, config), name="worker"
    )
    bot_task = asyncio.create_task(
        _run_with_restart("bot", bot_run_async, config), name="bot"
    )

    try:
        done, _ = await asyncio.wait(
            [bot_task, worker_task],
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in done:
            if task.exception():
                raise task.exception()  # type: ignore[misc]
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for task in [bot_task, worker_task]:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


def _run_setup(config) -> None:
    """Interactive Telethon session setup."""
    from telethon import TelegramClient

    logger = logging.getLogger("setup")
    logger.info("Setting up Telethon session: %s", config.TELETHON_SESSION)

    session_dir = os.path.dirname(config.TELETHON_SESSION)
    if session_dir:
        os.makedirs(session_dir, exist_ok=True)

    phone = input("מספר טלפון (עם קידומת מדינה, למשל +972501234567): ").strip()
    password = input("סיסמת 2FA (אם אין — השאר ריק ולחץ Enter): ").strip() or None

    async def _do_auth():
        client = TelegramClient(
            config.TELETHON_SESSION,
            config.TELETHON_API_ID,
            config.TELETHON_API_HASH,
        )
        await client.start(phone=phone, password=password)
        me = await client.get_me()
        logger.info(
            "Session created successfully for: %s (id=%s)",
            getattr(me, "username", getattr(me, "first_name", "?")),
            getattr(me, "id", "?"),
        )
        await client.disconnect()

        # Register this session as the default userbot and (re)activate it — the
        # worker may have marked it unauthorized when the session didn't exist yet.
        from app.repositories import userbot_repo
        ub = userbot_repo.ensure_default(config.TELETHON_SESSION, phone=phone)
        userbot_repo.update_identity(
            ub.id, getattr(me, "id", None), getattr(me, "username", None),
            name=getattr(me, "first_name", None) or ub.name,
        )
        userbot_repo.set_status(ub.id, "active", None)
        logger.info("Default userbot registered and activated (id=%d)", ub.id)

    asyncio.run(_do_auth())
    print("\nSession setup complete. You can now run: python main.py")
    print("Additional accounts can be added from the bot: ⚙️ הגדרות → 🤖 חשבונות יוזרבוט")


if __name__ == "__main__":
    main()
